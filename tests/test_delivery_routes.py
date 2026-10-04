"""Actual factory consumers use real domain producers and browser boundaries."""
import re
import json
from html.parser import HTMLParser
from xml.etree import ElementTree
from datetime import timedelta
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from course_platform.app import create_app
from course_platform.database import open_readonly, transaction
from course_platform.settings import Settings
import test_legacy_delivery as historical
from test_legacy_delivery import upgrade, owner_setup, activate, rows

original = historical.original




def test_paid_asset_is_not_available_from_preview(delivery_client):
    free = delivery_client.get("/courses/fixture-course/assets/free.png")
    paid = delivery_client.get("/courses/fixture-course/assets/paid.png")
    assert free.status_code == 200 and free.content == b"free image"
    assert paid.status_code in (403, 404)
    assert "script-src 'none'" in free.headers["content-security-policy"]
    page = delivery_client.get("/courses/fixture-course/chapters/1")
    assert 'src="/courses/fixture-course/assets/free.png"' in page.text
    assert 'href="/courses/fixture-course/assets/theme.css"' in page.text


def test_fourth_device_requires_blank_key_reentry_and_explicit_confirmation(delivery_client, issued_code, clock):
    client = delivery_client
    response = redeem(client, issued_code.raw_code)
    key = re.search(r"LK-[A-Za-z0-9_-]{43}", response.text).group()
    first_session = client.cookies.get("course_session_fixture-course")
    for _ in range(2):
        clock.advance(seconds=1)
        assert credential_post(client, "/access/restore", credential=key).status_code == 200
    blocked = credential_post(client, "/access/restore", credential=key)
    assert blocked.status_code == 409 and key not in blocked.text
    assert 'name="credential"' in blocked.text and 'name="confirm"' in blocked.text
    assert "checked" not in blocked.text and 'name="credential" value=' not in blocked.text
    token = client.cookies.get("coursmith_public_csrf")
    confirmed = client.post("/access/restore", data=dict(credential=key, confirm="on", csrf_token=token),
        headers={"Origin": "http://testserver"})
    assert confirmed.status_code == 200 and key not in confirmed.text
    assert client.app.state.access_service.get_session(first_session) is None


@pytest.mark.parametrize("format", ["pdf", "zip"])
def test_download_only_hub_and_original_policy_survive_archive(delivery_client, active_product, actor, format):
    from course_platform.operations.products import SalesChecklist
    client = delivery_client
    products = client.app.state.product_service
    data = active_product.data.model_copy(update={"policy": active_product.data.policy.model_copy(
        update={"online": False, "pdf": format == "pdf", "zip": format == "zip"})})
    revised = products.update(actor, active_product.id, active_product.revision, data)
    product = products.activate(actor, revised.id, revised.revision,
        SalesChecklist(quality=True, sources=True, ai=True, mobile=True, downloads=True))
    raw = client.app.state.access_service.create_access_code("fixture-course", actor=actor)
    products.set_status(actor, product.id, product.revision, "archived")
    response = redeem(client, raw)
    assert response.status_code == 200
    key = re.search(r"LK-[A-Za-z0-9_-]{43}", response.text).group()
    hub = client.get("/learn/fixture-course")
    assert hub.status_code == 200 and f'/downloads/course.{format}' in hub.text
    assert client.get(f"/learn/fixture-course/downloads/course.{format}").status_code == 200
    assert client.get("/learn/fixture-course/chapters/1").status_code == 403
    assert client.get("/learn/fixture-course/assets/free.png").status_code == 403
    assert client.get("/courses/fixture-course").status_code == 404
    assert credential_post(client, "/access/restore", credential=key).status_code == 200


def test_real_sale_owner_reset_public_restore_retains_exact_proof_and_progress(db_path, delivery_content, active_product, clock, monkeypatch):
    from course_platform.settings import Settings
    monkeypatch.setattr("course_platform.app.utc_now", clock.now)
    origin = "https://testserver"
    app = create_app(Settings("", "", delivery_content.parent, db_path, 48, "production", origin))
    with TestClient(app, base_url=origin, client=("198.51.100.8", 50001), follow_redirects=False) as buyer:
        # Independent jar on the already-running production factory.
        owner = TestClient(app, base_url=origin, client=("198.51.100.7", 50000), follow_redirects=False)
        try:
            page = owner.get("/admin/login")
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
            assert owner.post("/admin/login", data=dict(username="owner", password="example-pass-123", csrf_token=csrf),
                headers={"Origin": origin}).status_code == 303
            def owner_post(path, **data):
                return owner.post(path, data=dict(csrf_token=owner.cookies.get("coursmith_admin_csrf"), **data),
                    headers={"Origin": origin})
            recorded = owner_post("/admin/orders/new", channel="shop", shop_id="shop", external_order_id="real-sale-task11",
                product_id=str(active_product.id), paid_cents="100", paid_at=clock.now().isoformat(),
                note="Original shop payment checked", idempotency_key="record", confirm="on")
            assert recorded.status_code == 303
            issued = owner_post(recorded.headers["location"]+"/issue", revision="1", idempotency_key="issue")
            assert issued.status_code == 200 and origin in issued.text
            code = re.search(r"CS-[A-Za-z0-9_-]{32}", issued.text).group()
            response = redeem(buyer, code)
            assert response.status_code == 200 and "Secure" in response.headers["set-cookie"]
            old_key = re.search(r"LK-[A-Za-z0-9_-]{43}", response.text).group()
            old_session = buyer.cookies.get("course_session_fixture-course")
            csrf = buyer.cookies.get("course_csrf_fixture-course")
            assert buyer.post("/learn/fixture-course/chapters/1/progress", data=dict(csrf_token=csrf, completed="true"),
                headers={"Origin": origin}).status_code == 303
            with closing(open_readonly(db_path)) as connection:
                before = dict(connection.execute("SELECT id, expires_at, verified_at, verified_by, verified_reason FROM entitlements").fetchone())
                session_expiry = connection.execute("SELECT expires_at FROM sessions").fetchone()[0]
            assert session_expiry == (clock.now()+timedelta(hours=48)).isoformat()
            reset = owner_post(f"/admin/entitlements/{before['id']}/reset-credential", revision="1",
                reason="Checked original order holder", idempotency_key="public-reset", confirm="on")
            assert reset.status_code == 200
            key = re.search(r"LK-[A-Za-z0-9_-]{43}", reset.text).group()
            assert app.state.access_service.get_session(old_session) is None
            assert buyer.get("/learn/fixture-course/chapters/01.html").status_code == 403
            assert credential_post(buyer, "/access/restore", credential=old_key).status_code == 403
            assert credential_post(buyer, "/access/restore", credential=key).status_code == 200
            assert "已完成" in buyer.get("/learn/fixture-course").text
            assert owner.cookies.get("course_session_fixture-course") is None
            assert buyer.cookies.get("coursmith_admin") is None
            with closing(open_readonly(db_path)) as connection:
                assert dict(connection.execute("SELECT id, expires_at, verified_at, verified_by, verified_reason FROM entitlements").fetchone()) == before
        finally:
            owner.close()


def legacy_factory(original, clock, monkeypatch):
    monkeypatch.setattr("course_platform.app.utc_now", clock.now)
    app = create_app(Settings("", "", original.package.parent, original.db, 72, "test", "http://testserver"))
    return TestClient(app, client=("198.51.100.9", 50001), follow_redirects=False)


def test_genuine_original_short_code_owner_resolve_public_redeem(original, tmp_path, clock, monkeypatch):
    from test_product_routes import login, post
    upgrade(original, tmp_path)
    actor, _ = owner_setup(original, clock)
    activate(original, actor, clock)
    identity = rows(original.db, "SELECT id FROM access_codes WHERE used_at IS NULL AND expires_at IS NOT NULL")[0]["id"]
    with legacy_factory(original, clock, monkeypatch) as client:
        login(client)
        response = post(client, f"/admin/legacy-codes/{identity}/resolve", dict(revision="1", purpose="gift",
            verification_reason="Original holder checked", confirm="on", access_mode="days", access_days="30",
            online="on", update_policy="current_version"))
        assert response.status_code == 303
        response = redeem(client, original.unused)
        assert response.status_code == 200 and "LK-" in response.text
        assert client.get("/learn/fixture-course/chapters/01.html").status_code == 200
        assert redeem(client, original.unused).status_code == 403


def test_standalone_migrated_right_owner_verify_separate_reset_public_restore(original, tmp_path, clock, monkeypatch):
    from test_product_routes import login, post
    upgrade(original, tmp_path)
    owner_setup(original, clock)
    with legacy_factory(original, clock, monkeypatch) as client:
        client.cookies.set("course_session_fixture-course", original.sessions[0].session_id)
        before = rows(original.db, "SELECT expires_at FROM sessions")[0]["expires_at"]
        page = client.get("/learn/fixture-course")
        assert page.status_code == 200 and "已完成" in page.text
        token = client.cookies.get("course_csrf_fixture-course")
        assert token and rows(original.db, "SELECT csrf_hash FROM sessions")[0]["csrf_hash"]
        assert rows(original.db, "SELECT expires_at FROM sessions")[0]["expires_at"] == before
        assert client.post("/api/progress", json=dict(course_slug="fixture-course", chapter_number=1, completed=True,
            csrf_token=token), headers={"Origin": "http://testserver", "X-CSRF-Token": token}).status_code == 204
        login(client)
        verified = post(client, "/admin/entitlements/1/verify-legacy", dict(revision="1", purpose="gift",
            verification_reason="Individually verified original holder", idempotency_key="verify-public", confirm="on",
            access_mode="days", access_days="7", online="on", update_policy="current_version",
            expires_at=(clock.now()+timedelta(days=7)).isoformat()))
        assert verified.status_code == 303 and "LK-" not in verified.text
        reset = post(client, "/admin/entitlements/1/reset-credential", dict(revision="2", reason="Verified original holder",
            idempotency_key="reset-public", confirm="on"))
        assert reset.status_code == 200
        assert client.get("/learn/fixture-course").status_code == 403
        key = re.search(r"LK-[A-Za-z0-9_-]{43}", reset.text).group()
        assert credential_post(client, "/access/restore", credential=key).status_code == 200
        assert "已完成" in client.get("/learn/fixture-course").text


def credential_post(client, path, **data):
    client.get("/access/restore" if "restore" in path else "/access")
    return client.post(path, data={"csrf_token": client.cookies.get("coursmith_public_csrf"), **data},
        headers={"Origin": str(client.base_url).rstrip('/')})


def redeem(client, code):
    return credential_post(client, "/access/redeem", code=code, course_slug="fixture-course")


def test_progress_header_only_consumer(delivery_client, issued_code):
    client = delivery_client
    assert redeem(client, issued_code.raw_code).status_code == 200
    token = client.cookies.get("course_csrf_fixture-course")
    response = client.post("/api/progress", json=dict(course_slug="fixture-course", chapter_number=1,
        completed=True), headers={"Origin": "http://testserver", "X-CSRF-Token": token})
    assert response.status_code == 204
    assert "已完成" in client.get("/learn/fixture-course").text


@pytest.mark.parametrize("fault", ["missing", "wrong", "duplicate", "conflict", "empty", "wrong-session",
    "expired", "revoked", "online-off"])
def test_progress_header_denial_preserves_progress(delivery_client, issued_code, db_path, fault):
    client = delivery_client
    assert redeem(client, issued_code.raw_code).status_code == 200
    token = client.cookies.get("course_csrf_fixture-course")
    payload = dict(course_slug="fixture-course", chapter_number=1, completed=True)
    headers = [("Origin", "http://testserver"), ("X-CSRF-Token", token)]
    if fault == "missing":
        headers.pop()
        payload["csrf_token"] = token  # Body-only must never succeed.
    elif fault == "wrong":
        headers[-1] = ("X-CSRF-Token", "wrong")
    elif fault == "empty":
        headers[-1] = ("X-CSRF-Token", "")
    elif fault == "duplicate":
        headers.append(("x-csrf-token", token))
    elif fault == "conflict":
        payload["csrf_token"] = "different"
    elif fault == "wrong-session":
        with transaction(db_path) as conn:
            conn.execute("UPDATE sessions SET csrf_hash=?", ("a" * 64,))
    elif fault in ("expired", "revoked"):
        with transaction(db_path) as conn:
            conn.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'" if fault == "expired"
                else "UPDATE sessions SET revoked_at='2026-10-02T00:00:00+00:00'")
    else:
        with transaction(db_path) as conn:
            conn.execute("UPDATE entitlements SET issued_policy_json=json_set(issued_policy_json, '$.access.online', json('false'))")
    response = client.post("/api/progress", json=payload, headers=headers)
    assert response.status_code == 403
    with transaction(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM entitlement_progress").fetchone()[0] == 0


def test_no_js_progress_and_device_restore_share_progress(delivery_client, issued_code):
    client = delivery_client
    result = redeem(client, issued_code.raw_code)
    assert result.status_code == 200
    key = re.search(r"LK-[A-Za-z0-9_-]{43}", result.text).group()
    assert 'href="/learn/fixture-course"' in result.text
    assert "HttpOnly" in result.headers["set-cookie"] and "SameSite=lax" in result.headers["set-cookie"]
    page = client.get("/learn/fixture-course/chapters/1")
    assert 'method="post"' in page.text and "取消完成" not in page.text
    assert 'href="/learn/fixture-course/chapters/02.html"' in page.text and "下一章" in page.text
    second = client.get("/learn/fixture-course/chapters/02.html")
    assert "上一章" in second.text and 'src="/learn/fixture-course/assets/paid.png"' in second.text
    csrf = client.cookies.get("course_csrf_fixture-course")
    saved = client.post("/learn/fixture-course/chapters/1/progress", data={"csrf_token": csrf, "completed": "true"},
        headers={"Origin": "http://testserver"})
    assert saved.status_code == 303
    client.cookies.clear()
    restored = credential_post(client, "/access/restore", credential=key)
    assert restored.status_code == 200 and key not in restored.text
    assert "已完成" in client.get("/learn/fixture-course").text
    assert "取消完成" in client.get("/learn/fixture-course/chapters/1").text


def test_csrf_api_cannot_bypass_form_protection(delivery_client, issued_code):
    client = delivery_client
    assert redeem(client, issued_code.raw_code).status_code == 200
    payload = dict(course_slug="fixture-course", chapter_number=1, completed=True)
    assert client.post("/api/progress", json=payload, headers={"Origin": "http://testserver"}).status_code == 403
    payload["csrf_token"] = client.cookies.get("course_csrf_fixture-course")
    headers = {"Origin": "http://testserver", "X-CSRF-Token": payload["csrf_token"]}
    assert client.post("/api/progress", json=payload, headers=headers).status_code == 204
    payload["completed"] = "false"
    assert client.post("/api/progress", json=payload, headers=headers).status_code == 400


@pytest.mark.parametrize("path", ["chapters/1", "chapters/01.html", "chapters/index.html", "assets/free.png",
    "downloads/course.pdf", "downloads/course.zip"])
def test_all_legacy_and_current_routes_enforce_entitlement_revocation(delivery_client, issued_code, actor, db_path, path):
    client = delivery_client
    response = redeem(client, issued_code.raw_code)
    assert response.status_code == 200
    key = re.search(r"LK-[A-Za-z0-9_-]{43}", response.text).group()
    with transaction(db_path) as conn:
        identity = conn.execute("SELECT id FROM entitlements ORDER BY id DESC LIMIT 1").fetchone()[0]
    client.app.state.entitlement_service.revoke(actor, identity, 1, "External refund confirmed", "revoke")
    assert client.get("/learn/fixture-course/" + path).status_code == 403
    assert client.post("/api/progress", json=dict(course_slug="fixture-course", chapter_number=1, completed=True),
        headers={"Origin": "http://testserver", "X-CSRF-Token": client.cookies.get("course_csrf_fixture-course")}).status_code == 403
    assert credential_post(client, "/access/restore", credential=key).status_code == 403


def test_public_challenge_origin_generic_errors_and_shared_limit(delivery_client):
    client = delivery_client
    page = client.get("/access")
    token = client.cookies.get("coursmith_public_csrf")
    data = dict(csrf_token=token, code="CS-secret-invalid", course_slug="fixture-course")
    wrong = client.post("/access/redeem", data=data, headers={"Origin": "http://evil.test"})
    assert wrong.status_code == 403 and data["code"] not in wrong.text
    for i in range(9):
        response = credential_post(client, "/access/restore" if i % 2 else "/access/redeem", credential="LK-invalid", code="CS-invalid")
        assert response.status_code == 403
    assert credential_post(client, "/access/redeem", code="invalid").status_code == 429
    assert page.headers["cache-control"] == "no-store"


def test_download_exists_but_policy_denies_it(delivery_client, issued_code):
    client = delivery_client
    assert redeem(client, issued_code.raw_code).status_code == 200
    assert client.get("/learn/fixture-course/downloads/course.pdf").status_code == 403


def test_owner_dashboard_and_audit_are_real_private_projections(delivery_client):
    from test_product_routes import login
    client = delivery_client
    assert client.get("/admin").status_code == 303
    login(client)
    page = client.get("/admin")
    assert page.status_code == 200 and "待核验" in page.text and "交付" in page.text
    audit = client.get("/admin/audit?action=auth.login&page=1")
    assert audit.status_code == 200 and "auth.login" in audit.text
    assert "credential_hash" not in audit.text and "session_hash" not in audit.text
    assert audit.headers["cache-control"] == "no-store"


def test_dashboard_counts_real_recorded_delivery(delivery_client, active_product, actor, clock):
    from course_platform.operations.orders import OrderInput
    from test_product_routes import login
    client = delivery_client
    client.app.state.order_service.record(actor, OrderInput(channel="shop", shop_id="shop",
        external_order_id="pending-delivery", product_id=active_product.id, paid_cents=100,
        paid_at=clock.now(), note="Payment checked"), "pending")
    login(client)
    assert "<dt>待交付订单</dt><dd>1</dd>" in client.get("/admin").text


def test_unmatched_admin_errors_remain_private_with_admin_csp(delivery_client):
    response = delivery_client.get("/admin/not-a-route")
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert "script-src http://testserver/static/admin/" in response.headers["content-security-policy"]
    assert re.fullmatch("[0-9a-f]{32}", response.headers["x-request-id"])


def test_progress_denial_is_audited_without_secrets(delivery_client, issued_code, db_path):
    client = delivery_client
    redeem(client, issued_code.raw_code)
    token = client.cookies.get("course_session_fixture-course")
    denied = client.post("/api/progress", json=dict(course_slug="fixture-course", chapter_number=1, completed=True),
        headers={"Origin": "http://testserver"})
    assert denied.status_code == 403
    with closing(open_readonly(db_path)) as connection:
        events = [dict(row) for row in connection.execute("SELECT action, object_id, changes_json FROM admin_events WHERE action='progress.update' AND outcome='denied'")]
    assert len(events) == 1
    assert token not in str(events) and issued_code.raw_code not in str(events)


@pytest.mark.parametrize("kind", ["form-limit", "json-limit", "form-content", "json-content", "duplicate", "replay"])
def test_public_body_and_nonce_boundaries(delivery_client, issued_code, kind):
    client = delivery_client
    page = client.get("/access")
    assert page.status_code == 200
    token = client.cookies.get("coursmith_public_csrf")
    if kind == "form-limit":
        response = client.post("/access/redeem", content=b"x"*65537, headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://testserver"})
        assert response.status_code == 413
    elif kind == "json-limit":
        response = client.post("/api/progress", content=b"x"*16385, headers={"Content-Type": "application/json", "Origin": "http://testserver"})
        assert response.status_code == 413
    elif kind == "form-content":
        response = client.post("/access/redeem", json=dict(csrf_token=token, code=issued_code.raw_code), headers={"Origin": "http://testserver"})
        assert response.status_code == 415
    elif kind == "json-content":
        response = client.post("/api/progress", data=dict(course_slug="fixture-course"), headers={"Origin": "http://testserver"})
        assert response.status_code == 415
    elif kind == "duplicate":
        response = client.post("/access/redeem", content=f"csrf_token={token}&csrf_token={token}",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://testserver"})
        assert response.status_code == 400
    else:
        data = dict(csrf_token=token, code=issued_code.raw_code, course_slug="fixture-course")
        response = client.post("/access/redeem", data=data, headers={"Origin": "http://testserver"})
        assert response.status_code == 200
        response = client.post("/access/redeem", data=data, headers={"Origin": "http://testserver"})
        assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert issued_code.raw_code not in response.text


def test_delivery_content_corruption_is_unavailable_without_changing_baseline(delivery_client, issued_code, delivery_content, db_path):
    client = delivery_client
    assert redeem(client, issued_code.raw_code).status_code == 200
    with closing(open_readonly(db_path)) as connection:
        before = connection.execute("SELECT package_hash FROM courses").fetchone()[0]
    path = delivery_content / "chapters" / "01.html"
    path.write_text(path.read_text(encoding="utf8").replace("课程", "changed" )+" ", encoding="utf8")
    assert client.get("/learn/fixture-course/chapters/1").status_code == 503
    assert client.get("/courses/fixture-course/assets/free.png").status_code == 503
    with closing(open_readonly(db_path)) as connection:
        assert connection.execute("SELECT package_hash FROM courses").fetchone()[0] == before


def test_unexpected_error_is_sanitized_without_server_exception_or_secret_logs(delivery_client, issued_code, monkeypatch, caplog):
    client = delivery_client
    redeem(client, issued_code.raw_code)
    secret = "LK-private-exception-value"
    def failure(*args):
        raise RuntimeError(secret)
    monkeypatch.setattr(client.app.state.progress_service, "set_completed", failure)
    response = client.post("/api/progress", json=dict(course_slug="fixture-course", chapter_number=1,
        completed=True, csrf_token=client.cookies.get("course_csrf_fixture-course")),
        headers={"Origin": "http://testserver", "X-CSRF-Token": client.cookies.get("course_csrf_fixture-course")})
    assert response.status_code == 500 and "关联 ID" in response.text
    assert secret not in response.text and secret not in caplog.text
    assert all(record.exc_info is None for record in caplog.records if record.name == "course_platform.app")


def test_validated_body_layout_and_inline_css_resources_are_preserved(delivery_client, issued_code):
    client = delivery_client
    redeem(client, issued_code.raw_code)
    page = client.get("/learn/fixture-course/chapters/1")
    assert '<body class="learning-content"' in page.text
    assert 'background-image:url(&quot;/learn/fixture-course/assets/free.png&quot;)' in page.text


def test_paused_product_hides_purchase_keeps_preview_and_issued_access(delivery_client, issued_code, active_product, actor):
    client = delivery_client
    client.app.state.product_service.set_status(actor, active_product.id, active_product.revision, "paused")
    detail = client.get("/courses/fixture-course")
    assert detail.status_code == 200 and "暂停新销售" in detail.text
    assert "https://shop.example/course" not in detail.text
    assert "可售课程" in client.get("/").text
    assert client.get("/courses/fixture-course/chapters/1").status_code == 200
    assert redeem(client, issued_code.raw_code).status_code == 200
    assert client.get("/learn/fixture-course/chapters/1").status_code == 200


def test_audit_filters_paginate_twenty_minimal_events(delivery_client, active_product, actor):
    from test_product_routes import login
    client = delivery_client
    products = client.app.state.product_service
    record = products.create(actor, active_product.data.model_copy(update={"course_id": None}))
    for index in range(21):
        record = products.update(actor, record.id, record.revision, record.data.model_copy(update={"title": f"Draft {index}"}))
    login(client)
    params = dict(actor="1", action="product.update", object=str(record.id),
        **{"from": "2020-01-01T00:00:00+00:00", "to": "2030-01-01T00:00:00+00:00"})
    first = client.get("/admin/audit", params={**params, "page": "1"})
    second = client.get("/admin/audit", params={**params, "page": "2"})
    assert first.status_code == second.status_code == 200
    assert first.text.count(" · product.update · ") == 20
    assert second.text.count(" · product.update · ") == 1
    assert "共 21 条" in first.text and "action=product.update" in first.text
    assert "changes_json" not in first.text and "request_digest" not in first.text


def test_audit_query_exact_time_boundaries(delivery_client, db_path):
    from course_platform.audit import AuditEvent, append_event
    from test_product_routes import login
    from unittest.mock import patch
    from datetime import datetime, timezone
    with patch("course_platform.audit.utc_now", return_value=datetime(2026, 10, 2, tzinfo=timezone.utc)):
        with transaction(db_path) as conn:
            append_event(conn, AuditEvent(None, "request", "boundary", "learning.access", "denied", "denied",
                "boundary-time", {"error_code": "denied"}))
    login(delivery_client)
    for start, end, count in [("2026-10-02T00:00:00+00:00", "2026-10-02T00:00:00+00:00", 1),
        ("2026-10-02T00:00:00.000001+00:00", "2026-10-03T00:00:00+00:00", 0),
        ("2026-10-01T00:00:00+00:00", "2026-10-01T23:59:59.999999+00:00", 0)]:
        response = delivery_client.get("/admin/audit", params={"action": "learning.access", "from": start, "to": end})
        assert response.status_code == 200 and f"共 {count} 条" in response.text


@pytest.fixture
def nested_delivery_client(db_path, delivery_content, clock, monkeypatch, request):
    # Prepare legal bytes BEFORE any release/product/code is imported or issued.
    manifest_path = delivery_content / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    nested = delivery_content / "lessons" / "unit"
    nested.mkdir(parents=True)
    for chapter in manifest["chapters"]:
        filename = chapter["path"].split("/")[-1]
        chapter["path"] = "lessons/unit/" + filename
        image = "free" if chapter["free_preview"] else "paid"
        stylesheet = "theme" if chapter["free_preview"] else "paid"
        html = f'''<!doctype html><html><head><title>Chapter</title><style>
@import/**/"../../assets/{stylesheet}.css";
.image {{ background-image: image-set("../../assets/{image}.png" 1x); }}
.webkit {{ background-image: -webkit-image-set("../../assets/{image}.png" 1x); }}
.caption::after {{ content: "../../assets/not-a-resource.png"; }}
</style></head><body><div style='background-image:image-set("../../assets/{image}.png" 1x)'></div>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path id="first" d="M0 0 L1 1" cursor='image-set("../../assets/{image}.png" 1x),auto'/><path id="second" d="M2 2 L3 3"/></svg>
</body></html>'''
        (nested / filename).write_text(html, encoding="utf8")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf8")
    (delivery_content / "assets" / "paid.css").write_text('body { background:url("paid.png"); }', encoding="utf8")
    request.getfixturevalue("active_product")  # Real owner/product readiness, not test flags.
    monkeypatch.setattr("course_platform.app.utc_now", clock.now)
    app = create_app(Settings("", "", delivery_content.parent, db_path, 72, "test", "http://testserver"))
    with TestClient(app, client=("198.51.100.8", 50001), follow_redirects=False) as client:
        yield client


@pytest.mark.parametrize("prefix", ["/learn/fixture-course", "/courses/fixture-course"])
@pytest.mark.parametrize("alias", ["chapters/1", "chapters/01.html", "lessons/unit/01.html"])
def test_nested_css_string_resources_use_declared_base_and_membership(nested_delivery_client, actor, prefix, alias):
    import tinycss2
    client = nested_delivery_client
    assert client.get("/learn/fixture-course/chapters/1").status_code == 403
    raw = client.app.state.access_service.create_access_code("fixture-course", actor=actor)
    assert redeem(client, raw).status_code == 200
    response = client.get(prefix + "/" + alias)
    assert response.status_code == 200
    css = re.search(r"<style>(.*?)</style>", response.text, re.S).group(1)
    rules = tinycss2.parse_stylesheet(css, skip_whitespace=True, skip_comments=True)
    import_rule = next(rule for rule in rules if rule.type == "at-rule" and rule.lower_at_keyword == "import")
    imported = next(token.value for token in import_rule.prelude if token.type == "string")
    assert imported == prefix + "/assets/theme.css"
    for name in ("image-set", "-webkit-image-set"):
        assert f'{name}("{prefix}/assets/free.png" 1x)' in css
    assert 'content: "../../assets/not-a-resource.png"' in css  # Not every CSS string is a URL.

    class Styles(HTMLParser):
        def __init__(self):
            super().__init__()
            self.values = []

        def handle_starttag(self, tag, attrs):
            self.values.extend(value for name, value in attrs if name == "style")

    styles = Styles()
    styles.feed(response.text)
    assert f'image-set("{prefix}/assets/free.png" 1x)' in styles.values[0]
    assert client.get(imported).status_code == 200
    assert client.get(prefix + "/assets/free.png").content == b"free image"
    assert client.get("/courses/fixture-course/assets/paid.css").status_code == 404
    assert client.get("/courses/fixture-course/assets/paid.png").status_code == 404
    for paid_alias in ("chapters/2", "chapters/02.html", "lessons/unit/02.html"):
        assert client.get("/courses/fixture-course/" + paid_alias).status_code == 403
        assert client.get("/learn/fixture-course/" + paid_alias).status_code == 200
    assert client.get("/learn/fixture-course/assets/paid.css").status_code == 200
    assert client.get("/learn/fixture-course/assets/paid.png").content == b"paid image"
    client.cookies.clear()
    assert client.get("/learn/fixture-course/assets/free.png").status_code == 403
    assert client.get("/learn/fixture-course/assets/theme.css").status_code == 403


@pytest.mark.parametrize("prefix", ["/learn/fixture-course", "/courses/fixture-course"])
def test_validated_selfclosing_svg_paths_remain_siblings(nested_delivery_client, actor, prefix):
    client = nested_delivery_client
    raw = client.app.state.access_service.create_access_code("fixture-course", actor=actor)
    assert redeem(client, raw).status_code == 200
    response = client.get(prefix + "/chapters/1")
    assert response.status_code == 200
    svg = re.search(r"<svg\b.*?</svg>", response.text, re.S).group()
    try:
        element = ElementTree.fromstring(svg)
    except ElementTree.ParseError:
        pytest.fail("Validated self-closing SVG siblings were serialized as unclosed elements")
    assert [child.tag for child in element] == ["{http://www.w3.org/2000/svg}path"] * 2
    assert [child.attrib["id"] for child in element] == ["first", "second"]
    assert [child.attrib["d"] for child in element] == ["M0 0 L1 1", "M2 2 L3 3"]
    assert element[0].attrib["cursor"] == f'image-set("{prefix}/assets/free.png" 1x),auto'
    assert all(len(child) == 0 for child in element)


def test_nested_image_set_strings_resolve_from_declared_chapter(nested_delivery_client):
    response = nested_delivery_client.get("/courses/fixture-course/chapters/1")
    assert response.status_code == 200
    css = re.search(r"<style>(.*?)</style>", response.text, re.S).group(1)
    assert 'image-set("/courses/fixture-course/assets/free.png" 1x)' in css
    assert '-webkit-image-set("/courses/fixture-course/assets/free.png" 1x)' in css
