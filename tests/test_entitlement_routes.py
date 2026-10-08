"""Owner support HTTP tests use the shared app and real security services."""

import re
import asyncio
from threading import Event, Thread

import httpx
import pytest
from fastapi.testclient import TestClient

from course_platform.admin.routes.entitlements import router
from course_platform.database import transaction
from test_product_routes import login, post


@pytest.fixture
def entitlement_app(http_app_factory, admin_settings, admin_service, csrf_service, rate_limiter,
                    entitlement_service, recovery_service, progress_service, code_service, product_service):
    from course_platform.admin.routes.auth import router as auth_router
    from course_platform.admin.routes.codes import router as code_router
    from course_platform.admin.routes.products import router as product_router
    return http_app_factory([auth_router, product_router, code_router, router], admin_settings, {
        "admin_service": admin_service, "csrf_service": csrf_service, "rate_limiter": rate_limiter,
        "entitlement_service": entitlement_service, "recovery_service": recovery_service,
        "progress_service": progress_service, "code_service": code_service, "product_service": product_service})


@pytest.fixture
def entitlement_client(entitlement_app):
    with TestClient(entitlement_app, client=("198.51.100.7", 50000), follow_redirects=False) as client:
        yield client


def values(**extra):
    return {"revision": "1", "reason": "已核对发行记录，凭证遗失", "idempotency_key": "http-reset",
            "confirm": "on", **extra}


def test_private_detail_reset_and_metadata_replay(entitlement_client, verified_gift, db_path):
    login(entitlement_client)
    detail = entitlement_client.get("/admin/entitlements/1")
    assert detail.status_code == 200 and detail.headers["cache-control"] == "no-store"
    assert 'name="confirm"' in detail.text and "Asia/Shanghai" in detail.text
    response = post(entitlement_client, "/admin/entitlements/1/reset-credential", values())
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    raw = re.search(r"LK-[A-Za-z0-9_-]{43}", response.text).group()
    replay = post(entitlement_client, "/admin/entitlements/1/reset-credential", values())
    assert replay.status_code == 200 and raw not in replay.text and "再次重置" in replay.text
    assert raw not in entitlement_client.get("/admin/entitlements/1").text
    assert raw.encode() not in db_path.read_bytes()


@pytest.mark.parametrize("operation,action", [("reset-credential", "recovery.reset"), ("revoke", "entitlement.revoke")])
@pytest.mark.parametrize("fault,status", [("auth", 401), ("origin", 403), ("csrf", 403), ("body", 413), ("revision", 409), ("confirm", 400)])
def test_boundary_denials_have_one_safe_domain_audit(entitlement_client, verified_gift, db_path,
        operation, action, fault, status):
    if fault != "auth":
        login(entitlement_client)
    data = values(revision="bad" if fault == "revision" else "1", confirm="" if fault == "confirm" else "on")
    data["csrf_token"] = "bad" if fault == "csrf" else entitlement_client.cookies.get("coursmith_admin_csrf", "")
    headers = {"Origin": "https://evil.example" if fault == "origin" else "http://testserver"}
    path = f"/admin/entitlements/1/{operation}"
    response = entitlement_client.post(path, content=b"x" * 65537, headers={**headers,
        "Content-Type": "application/x-www-form-urlencoded"}) if fault == "body" else entitlement_client.post(path, data=data, headers=headers)
    assert response.status_code == status and response.headers["cache-control"] == "no-store"
    with transaction(db_path) as connection:
        events = connection.execute("SELECT object_type, object_id FROM admin_events WHERE action=? AND outcome='denied'", (action,)).fetchall()
        assert [tuple(row) for row in events] == [("entitlement", "1")]
        assert connection.execute("SELECT revision, revoked_at FROM entitlements").fetchone()[0] == 1


def test_revoke_is_confirmed_revisioned_and_replayable(entitlement_client, verified_gift):
    login(entitlement_client)
    for _ in range(2):
        response = post(entitlement_client, "/admin/entitlements/1/revoke", values())
        assert response.status_code == 303 and response.headers["location"] == "/admin/entitlements/1"
    assert entitlement_client.get("/admin/entitlements/1").status_code == 200
    assert post(entitlement_client, "/admin/entitlements/1/reset-credential", values(revision="2", idempotency_key="new")).status_code == 403


def test_detail_entry_is_from_redeemed_code_not_dead_collection(entitlement_client, verified_gift):
    login(entitlement_client)
    batch = entitlement_client.get("/admin/code-batches/1")
    assert 'href="/admin/entitlements/1"' in batch.text
    detail = entitlement_client.get("/admin/entitlements/1")
    assert detail.status_code == 200 and 'href="/admin/entitlements"' not in detail.text


def test_detail_fetches_only_safe_fields(entitlement_client, verified_gift, db_path, monkeypatch):
    from course_platform.delivery import recovery
    login(entitlement_client)
    queries = []
    original = recovery.open_readonly
    def traced(path):
        connection = original(path)
        connection.set_trace_callback(queries.append)
        return connection
    monkeypatch.setattr(recovery, "open_readonly", traced)
    with transaction(db_path) as connection:
        secrets = [row[0] for row in connection.execute("SELECT credential_hash FROM recovery_credentials")]
        secrets += [row[0] for row in connection.execute("SELECT session_hash FROM sessions")]
        secrets += [row[0] for row in connection.execute("SELECT package_hash FROM entitlements")]
    page = entitlement_client.get("/admin/entitlements/1")
    assert page.status_code == 200
    assert all(secret not in page.text for secret in secrets)
    assert all("select *" not in query.lower() and "credential_hash" not in query.lower()
               and "session_hash" not in query.lower() and "csrf_hash" not in query.lower() for query in queries)


@pytest.mark.parametrize("operation", ["reset", "revoke", "record_denial", "verify_bound_csrf"])
def test_sync_support_and_security_calls_are_offloaded(entitlement_app, entitlement_client,
        recovery_service, entitlement_service, csrf_service, verified_gift, monkeypatch, operation):
    login(entitlement_client)
    service = csrf_service if operation == "verify_bound_csrf" else entitlement_service if operation == "revoke" else recovery_service
    gate, release = Event(), Event()
    original = getattr(service, operation)
    def delayed(*args, **kwargs):
        gate.set()
        assert release.wait(5), "Security or DB work blocked the event loop"
        return original(*args, **kwargs)
    monkeypatch.setattr(service, operation, delayed)
    path = "/admin/entitlements/1/revoke" if operation == "revoke" else "/admin/entitlements/1/reset-credential"
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=entitlement_app), base_url="http://testserver",
                cookies=dict(entitlement_client.cookies.items())) as client:
            pending = asyncio.create_task(client.post(path, data={"csrf_token": entitlement_client.cookies.get("coursmith_admin_csrf"),
                **values(revision="bad" if operation == "record_denial" else "1")}, headers={"Origin": "http://testserver"}))
            assert await asyncio.to_thread(gate.wait, 3)
            try:
                assert (await asyncio.wait_for(client.get("/static/admin/admin.css"), 1)).status_code == 200
                assert not release.is_set()
            finally:
                release.set()
            response = await pending
            assert response.status_code == (409 if operation == "record_denial" else 303 if operation == "revoke" else 200)
    watchdog = Thread(target=lambda: (release.wait(4), release.set()), daemon=True)
    watchdog.start()
    try:
        asyncio.run(scenario())
    finally:
        release.set()
        watchdog.join()


def test_pending_legacy_is_readable_but_cannot_be_reset(entitlement_client, active_product, db_path):
    with transaction(db_path) as connection:
        connection.execute("""INSERT INTO entitlements (course_id, course_version, legacy_state)
            VALUES ('fixture-course', '0.1.0', 'pending_verification')""")
    login(entitlement_client)
    response = entitlement_client.get("/admin/entitlements/1")
    assert response.status_code == 200 and "待核验" in response.text
    assert "无固定到期日" not in response.text
    assert post(entitlement_client, "/admin/entitlements/1/reset-credential", values()).status_code == 409
    with transaction(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM recovery_credentials").fetchone()[0] == 0


@pytest.mark.parametrize("secret_kind", ["recovery", "session", "hash"])
def test_detail_never_displays_unsafe_verification_history(entitlement_client, verified_gift, db_path, secret_kind):
    secret = {"recovery": verified_gift.raw_recovery_key, "session": verified_gift.session.session_id,
              "hash": "a" * 64}[secret_kind]
    with transaction(db_path) as connection:
        connection.execute("UPDATE entitlements SET verified_reason=?", (secret,))
    login(entitlement_client)
    response = entitlement_client.get("/admin/entitlements/1")
    assert response.status_code == 200 and secret not in response.text


def test_stream_limit_duplicate_fields_invalid_identity_and_missing_rights(entitlement_app,
        entitlement_client, verified_gift, db_path):
    login(entitlement_client)
    chunks = []
    async def stream():
        for index in range(3):
            chunks.append(index)
            yield b"x" * 40000
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=entitlement_app), base_url="http://testserver",
                cookies=dict(entitlement_client.cookies.items())) as client:
            return await client.post("/admin/entitlements/1/reset-credential", content=stream(), headers={
                "Origin": "http://testserver", "Content-Type": "application/x-www-form-urlencoded"})
    response = asyncio.run(scenario())
    assert response.status_code == 413 and chunks == [0, 1]
    duplicate = entitlement_client.post("/admin/entitlements/1/reset-credential", content="confirm=on&confirm=on",
        headers={"Origin": "http://testserver", "Content-Type": "application/x-www-form-urlencoded"})
    assert duplicate.status_code == 400
    invalid = post(entitlement_client, "/admin/entitlements/LK-untrusted/revoke", values())
    assert invalid.status_code == 400 and "LK-untrusted" not in invalid.text
    assert entitlement_client.get("/admin/entitlements/999").status_code == 404
    assert entitlement_client.get("/admin/entitlements/1/revoke").status_code == 405
    with transaction(db_path) as connection:
        events = connection.execute("SELECT object_id, action FROM admin_events WHERE outcome='denied' AND action IN ('recovery.reset','entitlement.revoke')").fetchall()
        assert [tuple(row) for row in events] == [("1", "recovery.reset"), ("1", "recovery.reset"), ("invalid", "entitlement.revoke")]
        assert connection.execute("SELECT revision FROM entitlements").fetchone()[0] == 1


def test_buyer_cookie_does_not_authorize_support(entitlement_client, verified_gift):
    entitlement_client.cookies.set("course_session_fixture-course", verified_gift.session.session_id)
    response = entitlement_client.get("/admin/entitlements/1")
    assert response.status_code == 303 and response.headers["location"] == "/admin/login"
    assert post(entitlement_client, "/admin/entitlements/1/reset-credential", values()).status_code == 401
