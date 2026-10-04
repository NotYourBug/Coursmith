"""Black-box release boundaries, persistence and an actual matched-copy restore."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import re
import shutil
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from course_platform.app import create_app
from course_platform.database import backup_database, check_database, open_readonly, transaction
from course_platform.domain import Actor
from course_platform.operations.codes import BatchInput, CodeService
from course_platform.operations.products import AccessPolicy, ProductService, SalesChannel, SalesChecklist
from course_platform.settings import Settings
from test_delivery_routes import credential_post, redeem
from test_product_routes import login, post
import test_legacy_delivery as historical

original_restore_history = historical.original


@pytest.mark.parametrize("original_state", ["changed", "absent"])
def test_runbook_pre_v5_conversion_uses_matched_copied_bytes(original_restore_history, tmp_path, clock, monkeypatch, original_state):
    """Execute the documented ordering, not a correct order invented by the test."""
    import subprocess
    import sys
    import os
    import shlex
    from course_platform.content_inspection import inspect_package

    original = original_restore_history
    before = snapshot(original.source, ("courses", "chapters", "access_codes", "sessions", "progress", "events"))
    root = tmp_path / "handoff-copy"
    copied = root / "content" / "fixture-course"
    sealed = root / "old-compatible-content" / "fixture-course"
    shutil.copytree(original.package, copied)
    shutil.copytree(original.package, sealed)
    inventory = lambda p: {f.relative_to(p).as_posix(): f.read_bytes() for f in p.rglob("*") if f.is_file()}
    assert inventory(copied) == inventory(sealed) == inventory(original.package)
    fingerprint = inspect_package(copied).fingerprint
    db, compatible = root / "working.db", root / "old-compatible.db"
    backup_database(original.source, db)
    backup_database(original.source, compatible)
    text = (Path(__file__).resolve().parents[1] / "docs/operations/admin-v1-runbook.md").read_text(encoding="utf8")
    section = text.split("## 旧库维护窗口、匹配备份、切换与回滚")[1].split("## 验收命令及环境限制")[0]
    if "### pre-v5" in section:
        section = section.split("### pre-v5", 1)[1].split("### ", 1)[0]
    # In the original handoff, the inline migrate command precedes the Python
    # relocation example. The corrected historical branch must reverse that.
    steps = re.finditer(r"```python\n(?P<python>.*?)```|`(?P<cli>[^`\n]*migrate --backup[^`\n]*)`", section, re.S)
    saved_bytes = (original.package / "chapters/01.html").read_bytes()
    hidden = original.package.with_name("original-withheld")
    if original_state == "changed":
        (original.package / "chapters/01.html").write_text("CHANGED ORIGINAL BEFORE CONVERSION", encoding="utf8")
    else:
        original.package.rename(hidden)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update(COURSE_DATABASE=str(db), COURSE_CONTENT_ROOT=str(root / "content"), COURSE_ENVIRONMENT="test",
               COURSE_SITE_ORIGIN="http://127.0.0.1:8000", DEEPSEEK_API_KEY="")
    executed = []
    try:
        for step in steps:
            if step["python"] is not None:
                code = step["python"]
                if "copied_db =" not in code:
                    continue  # Separate backup-only example: fixture already made nonoverwriting copies.
                for label, value in {"<副本DB绝对路径>": db, "<副本内容根绝对路径>": root / "content",
                                     "<兼容快照DB绝对路径>": compatible,
                                     "<兼容快照内容根绝对路径>": root / "old-compatible-content",
                                     "<该课程目录>": "fixture-course"}.items():
                    code = code.replace(label, str(value).replace("\\", "/"))
                try:
                    exec(compile(code, "runbook-relocation", "exec"), {})
                except Exception as exc:
                    pytest.fail("Documented relocation cannot consume the matched old backup after the documented order: " + type(exc).__name__)
                executed.append("relocation")
            else:
                # I2 isolates order/content semantics; I1 separately executes the
                # documented interpreter in a genuinely installed environment.
                args = shlex.split(step["cli"].split("migrate --backup", 1)[1].strip())
                assert args == ["<副本升级前的非覆盖备份>"]
                result = subprocess.run([sys.executable, "-m", "course_platform.cli", "migrate", "--backup", str(root / "before-v5.db")],
                    env=env, capture_output=True, text=True, timeout=30)
                assert result.returncode == 0, "Documented offline conversion failed"
                executed.append("migration")
        assert executed == ["relocation", "migration"], "Historical conversion must follow verified copied-only relocation"
        assert check_database(db)["version"] == 5 and check_database(db)["foreign_keys"] == "ok"
        with closing(open_readonly(db)) as c:
            assert {tuple(row) for row in c.execute("SELECT content_available,package_hash FROM legacy_entitlement_origins")} == {(1, fingerprint)}
            assert [tuple(row) for row in c.execute("SELECT session_hash,course_id,created_at,expires_at FROM sessions ORDER BY rowid")] == before["sessions"]
            assert [tuple(row) for row in c.execute("SELECT * FROM progress ORDER BY rowid")] == before["progress"]
            assert c.execute("SELECT count(*) FROM entitlements WHERE source_code_id IS NOT NULL OR verified_at IS NOT NULL OR issued_policy_json IS NOT NULL").fetchone()[0] == 0
        monkeypatch.setattr("course_platform.app.utc_now", clock.now)
        app = create_app(Settings("", "", root / "content", db, 72, "test", "http://testserver"))
        with TestClient(app, client=("198.51.100.8", 50001)) as client:
            client.cookies.set("course_session_fixture-course", original.sessions[0].session_id)
            page = client.get("/learn/fixture-course/chapters/1")
            assert page.status_code == 200 and "取消完成" in page.text and "CHANGED ORIGINAL BEFORE CONVERSION" not in page.text
    finally:
        if original_state == "absent":
            hidden.rename(original.package)
        else:
            (original.package / "chapters/01.html").write_bytes(saved_bytes)
    assert snapshot(original.source, tuple(before)) == snapshot(compatible, tuple(before)) == before
    assert inventory(sealed) == inventory(copied)
    print(f"Documented pre-v5 conversion passed with original {original_state}; matched old-compatible DB/content unchanged")


def make_sale_ready(site):
    actor = Actor(1, "release-owner")
    products = ProductService(site.db)
    cat = products.save_category(actor, None, None, "release-technical", "技术", 0, True)
    draft = products.get_product(1)
    data = draft.data.model_copy(update=dict(category_id=cat, synopsis="Course", audience="Learners", prerequisites="None",
        outcomes=["Practice"], ai_disclosure="AI assisted", support_text="Shop support",
        channels=[SalesChannel(name="Shop", url="https://shop.example/course")],
        policy=AccessPolicy(access_mode="days", access_days=30, online=True, pdf=True, zip=True,
                            update_policy="current_version")))
    updated = products.update(actor, draft.id, draft.revision, data)
    products.activate(actor, updated.id, updated.revision,
        SalesChecklist(quality=True, sources=True, ai=True, mobile=True, downloads=True))
    return site


@pytest.fixture
def ready_site(live_site):
    return make_sale_ready(live_site)


def socket_login(client, site):
    page = client.get("/admin/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    result = client.post("/admin/login", data=dict(username="owner", password="example-pass-123", csrf_token=token),
                         headers={"Origin": site.origin})
    assert result.status_code == 303
    return result


def socket_post(client, site, path, **data):
    return client.post(path, data=dict(csrf_token=client.cookies.get("coursmith_admin_csrf"), **data),
                       headers={"Origin": site.origin})


def socket_public(client, site, path, **data):
    client.get("/access/restore" if "restore" in path else "/access")
    return client.post(path, data=dict(csrf_token=client.cookies.get("coursmith_public_csrf"), **data),
                       headers={"Origin": site.origin})


def sale_receipt(owner, buyer, site):
    socket_login(owner, site)
    from datetime import datetime, timezone
    recorded = socket_post(owner, site, "/admin/orders/new", channel="shop", shop_id="shop",
        external_order_id="release-private-order", product_id="1", paid_cents="100",
        paid_at=datetime.now(timezone.utc).isoformat(), note="Checked payment", idempotency_key="record", confirm="on")
    assert recorded.status_code == 303
    issued = socket_post(owner, site, recorded.headers["location"] + "/issue", revision="1", idempotency_key="issue")
    assert issued.status_code == 200
    code = re.search(r"CS-[A-Za-z0-9_-]{32}", issued.text).group()
    redeemed = socket_public(buyer, site, "/access/redeem", code=code, course_slug="fixture-course")
    assert redeemed.status_code == 200
    key = re.search(r"LK-[A-Za-z0-9_-]{43}", redeemed.text).group()
    return code, key


def snapshot(path, tables=None):
    with closing(open_readonly(path)) as c:
        tables = tables or ("admins", "categories", "products", "orders", "code_batches", "access_codes",
                            "entitlements", "recovery_credentials", "entitlement_progress", "legacy_verifications", "operation_requests")
        return {name: [tuple(row) for row in c.execute(f"SELECT * FROM {name} ORDER BY rowid")] for name in tables}


POST_ROUTES = [
    "/admin/login", "/admin/logout", "/admin/account/password", "/admin/products/new", "/admin/products/{product_id}",
    "/admin/products/{product_id}/activate", "/admin/products/{product_id}/pause", "/admin/products/{product_id}/archive",
    "/admin/categories", "/admin/categories/{category_id}", "/admin/code-batches/new", "/admin/codes/{code_id}/revoke",
    "/admin/codes/{code_id}/replace", "/admin/code-batches/{batch_id}/revoke-unused", "/admin/code-batches/{batch_id}/replace-unused",
    "/admin/orders/search", "/admin/orders/new", "/admin/orders/{order_id}/issue", "/admin/orders/{order_id}/confirm-delivery",
    "/admin/orders/{order_id}/attach-code", "/admin/orders/{order_id}/refund", "/admin/entitlements/{entitlement_id}/reset-credential",
    "/admin/entitlements/{entitlement_id}/revoke", "/admin/legacy-codes/{code_id}/resolve", "/admin/entitlements/{entitlement_id}/verify-legacy",
    "/access/redeem", "/access/restore", "/learn/{slug}/chapters/{number}/progress", "/api/progress",
]


def test_every_mutating_route_rejects_missing_csrf(ready_site):
    site = ready_site
    registered = {path for path, methods in create_app().openapi()["paths"].items() if "post" in methods}
    assert registered == set(POST_ROUTES), "New POST endpoints need explicit release acceptance"
    with httpx.Client(base_url=site.url, verify=False, trust_env=False, timeout=10) as owner, httpx.Client(base_url=site.url, verify=False, trust_env=False, timeout=10) as buyer, httpx.Client(base_url=site.url, verify=False, trust_env=False, timeout=10) as anonymous:
        code, key = sale_receipt(owner, buyer, site)
        spare = CodeService(site.db).issue_batch(Actor(1, "csrf-spare"), BatchInput(product_id=1, purpose="gift"), "spare").codes[0]
        with closing(open_readonly(site.db)) as c:
            spare_id = c.execute("SELECT id FROM access_codes WHERE code_hash=?", (hashlib.sha256(spare.raw_code.encode()).hexdigest(),)).fetchone()[0]
        for identity, client in (("anonymous", anonymous), ("buyer", buyer), ("owner", owner)):
            client.get("/admin/login")
            client.get("/access")
            for template in POST_ROUTES:
                path = template.format(number=1, product_id=1, category_id=1, code_id=spare_id, batch_id=2, order_id=1,
                                       entitlement_id=1, slug="fixture-course")
                data = dict(revision="1", idempotency_key="missing-csrf", confirm="on", code=code, credential=key,
                    course_slug="fixture-course", chapter_number=1, completed=True, username="owner", password="example-pass-123")
                before = snapshot(site.db)
                response = client.post(path, **({"json": data} if path == "/api/progress" else {"data": data}),
                                       headers={"Origin": site.origin})
                assert response.status_code in (401, 403, 429), (identity, path, response.status_code)
                assert snapshot(site.db) == before, (identity, path)


@pytest.mark.parametrize("trusted,cli", [("", True), ("127.0.0.0/8,10.0.0.0/8", False)])
def test_real_server_socket_peer_controls_shared_bucket(server_factory, release_content, tmp_path, trusted, cli):
    from course_platform.database import migrate_database
    db = tmp_path / "proxy.db"
    migrate_database(db)
    with server_factory(release_content.parent, db, trusted=trusted, cli=cli) as site, httpx.Client(base_url=site.url, trust_env=False, timeout=10) as client:
        for n in range(11):
            # Left spoofed address changes; first untrusted hop on the right stays.
            headers = {"Origin": site.origin, "X-Forwarded-For": f"203.0.113.{n+1}, 198.51.100.7, 10.1.1.1",
                       "Forwarded": f"for=203.0.113.{n+1};proto=https", "X-Real-IP": f"203.0.113.{n+1}"}
            client.get("/access")
            response = client.post("/access/redeem" if n % 2 else "/access/restore",
                data=dict(code="invalid", credential="invalid", csrf_token=client.cookies.get("coursmith_public_csrf")), headers=headers)
            assert response.status_code == (403 if n < 10 else 429)
        expected_peer = "198.51.100.7" if trusted else "127.0.0.1"
        digest = hashlib.sha256(hashlib.sha256(expected_peer.encode()).hexdigest().encode()).hexdigest()
        with closing(open_readonly(db)) as c:
            assert [tuple(row) for row in c.execute("SELECT source_digest,count FROM request_limits WHERE scope='public_credentials'")] == [(digest, 10)]
        if trusted:
            # A different first untrusted peer gets its own bucket; left hops cannot impersonate it.
            response = client.post("/access/restore", data=dict(credential="invalid"),
                headers={"Origin": site.origin, "X-Forwarded-For": "203.0.113.4, 198.51.100.8, 10.1.1.1"})
            assert response.status_code == 403
            assert client.post("/access/restore", data=dict(credential="invalid"),
                headers={"Origin": site.origin, "X-Forwarded-For": "invalid"}).status_code == 400


def test_real_server_origin_secure_cookies_and_private_errors(server_factory, release_content, tmp_path):
    from course_platform.database import migrate_database
    from course_platform.admin.auth import AdminService
    db = tmp_path / "production.db"
    migrate_database(db)
    AdminService(db).initialize_owner("owner", "example-pass-123")
    with server_factory(release_content.parent, db, production=True) as site, httpx.Client(base_url=site.url, trust_env=False, timeout=10) as client:
        page = client.get("/admin/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        cookie = f"coursmith_admin_login_csrf={token}"
        rejected = client.post("/admin/login", data=dict(username="owner", password="example-pass-123", csrf_token=token),
            headers={"Origin": "https://evil.example", "Cookie": cookie, "X-Forwarded-Host": "courses.example"})
        assert rejected.status_code == 403
        page = client.get("/admin/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        result = client.post("/admin/login", data=dict(username="owner", password="example-pass-123", csrf_token=token),
            headers={"Origin": site.origin, "Cookie": f"coursmith_admin_login_csrf={token}"})
        assert result.status_code == 303
        cookies = result.headers.get_list("set-cookie")
        assert all("Secure" in value and "HttpOnly" in value and "Path=/admin" in value and "SameSite=strict" in value for value in cookies)
        for method, path, status in (("GET", "/admin/not-found", 404), ("DELETE", "/admin/login", 405),
                                     ("GET", "/learn/fixture-course/chapters/1", 403)):
            response = client.request(method, path)
            assert response.status_code == status and response.headers["cache-control"] == "no-store"
            assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])
            assert "script-src https://courses.example/static/admin/" in response.headers["content-security-policy"] if path.startswith("/admin") else "script-src 'none'" in response.headers["content-security-policy"]
        challenge = client.get("/access")
        assert "Secure" in challenge.headers["set-cookie"] and "SameSite=lax" in challenge.headers["set-cookie"]
        make_sale_ready(site)
        code = CodeService(db).issue_batch(Actor(1, "production-cookie"), BatchInput(product_id=1, purpose="gift"), "production-cookie").codes[0].raw_code
        token = re.search(r'name="csrf_token" value="([^"]+)"', challenge.text).group(1)
        grant = client.post("/access/redeem", data=dict(code=code, csrf_token=token),
            headers={"Origin": site.origin, "Cookie": f"coursmith_public_csrf={token}"})
        assert grant.status_code == 200 and "LK-" in grant.text
        buyer_cookies = [value for value in grant.headers.get_list("set-cookie") if value.startswith(("course_session_", "course_csrf_"))]
        assert len(buyer_cookies) == 2
        assert all("Secure" in value and "HttpOnly" in value and "Path=/;" in value and "SameSite=lax" in value for value in buyer_cookies)
        assert all(259190 <= int(re.search(r"Max-Age=(\d+)", value).group(1)) <= 259200 for value in buyer_cookies)
        assert grant.headers["cache-control"] == "no-store" and "script-src 'none'" in grant.headers["content-security-policy"]
        with transaction(db) as c:
            c.execute("DROP TABLE csrf_challenges")
        error = client.get("/access")
        assert error.status_code == 500 and error.headers["cache-control"] == "no-store"
        assert "csrf_challenges" not in error.text and "Traceback" not in error.text
        log = site.log.read_text(encoding="utf8")
        assert "request_failed type=OperationalError request_id=" in log
        assert "Traceback" not in log and "example-pass-123" not in log and "no such table" not in log


def test_real_login_and_public_challenges_are_one_shot_and_limited(ready_site):
    site = ready_site
    with httpx.Client(base_url=site.url, verify=False, trust_env=False, timeout=10) as client:
        page = client.get("/admin/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        # Foreign/null/missing Origin never establishes an owner session.
        for origin in (None, "null", "https://evil.example"):
            headers = {"Origin": origin} if origin else {}
            response = client.post("/admin/login", data=dict(username="owner",password="example-pass-123",csrf_token=token), headers=headers)
            assert response.status_code == 403 and not client.cookies.get("coursmith_admin")
        for n in range(6):
            page = client.get("/admin/login")
            token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
            response = client.post("/admin/login", data=dict(username="owner",password="wrong-password-123",csrf_token=token),
                                   headers={"Origin": site.origin, "X-Forwarded-For": f"198.51.100.{n+1}"})
            assert response.status_code == (401 if n < 5 else 429)
        with closing(open_readonly(site.db)) as c:
            assert [tuple(row) for row in c.execute("SELECT scope,count FROM request_limits ORDER BY scope")] == [("login_account",5),("login_source",5)]
        client.get("/access")
        public_token = client.cookies.get("coursmith_public_csrf")
        cookie = f"coursmith_public_csrf={public_token}"
        first = client.post("/access/redeem", data=dict(code="invalid",csrf_token=public_token),
                            headers={"Origin": site.origin,"Cookie":cookie})
        assert first.status_code == 403
        for path in ("/access/redeem", "/access/restore"):
            response = client.post(path, data=dict(code="invalid",credential="invalid",csrf_token=public_token),
                                   headers={"Origin":site.origin,"Cookie":cookie})
            assert response.status_code == 403
        with closing(open_readonly(site.db)) as c:
            assert c.execute("SELECT count(*) FROM entitlements").fetchone()[0] == 0
            assert c.execute("SELECT count FROM request_limits WHERE scope='public_credentials'").fetchone()[0] == 3


def test_secrets_are_absent_from_persistence_logs_and_artifacts(ready_site):
    site = ready_site
    with httpx.Client(base_url=site.url, verify=False, trust_env=False, timeout=10) as owner, httpx.Client(base_url=site.url, verify=False, trust_env=False, timeout=10) as buyer:
        code, key = sale_receipt(owner, buyer, site)
        secrets = [code, key, "example-pass-123", owner.cookies.get("coursmith_admin"), buyer.cookies.get("course_session_fixture-course")]
        replay = socket_post(owner, site, "/admin/orders/1/issue", revision="1", idempotency_key="issue")
        assert replay.status_code == 200 and code not in replay.text
        lost_reset = socket_post(owner, site, "/admin/entitlements/1/reset-credential", revision="1",
            idempotency_key="reset", reason="Verified original order holder", confirm="on")
        new_key = re.search(r"LK-[A-Za-z0-9_-]{43}", lost_reset.text).group()
        secrets.append(new_key)
        reset_replay = socket_post(owner, site, "/admin/entitlements/1/reset-credential", revision="1",
            idempotency_key="reset", reason="Verified original order holder", confirm="on")
        assert reset_replay.status_code == 200 and new_key not in reset_replay.text
        persistent_pages = []
        for path in ("/admin", "/admin/code-batches/1", "/admin/entitlements/1", "/admin/audit", "/admin/orders", "/access/restore"):
            response = owner.get(path)
            assert response.status_code == 200
            persistent_pages.append(response.text)
        with closing(open_readonly(site.db)) as c:
            dump = "\n".join(c.iterdump())
            audit = str([tuple(row) for row in c.execute("SELECT * FROM admin_events")])
            history = str([tuple(row) for row in c.execute("SELECT * FROM operation_requests")])
        files = [path.read_bytes() for path in site.content.parent.rglob("*") if path.is_file()]
        for secret in secrets:
            assert secret not in dump
            assert secret.encode() not in site.db.read_bytes()
            assert all(secret.encode() not in value for value in files)
            assert secret not in site.log.read_text(encoding="utf8")
            assert all(secret not in page for page in persistent_pages)
            digest = hashlib.sha256(secret.encode()).hexdigest()
            assert digest not in audit + history + "".join(persistent_pages)
        assert "release-private-order" not in audit + history + site.log.read_text(encoding="utf8")
        assert "release-private-order" not in "".join(persistent_pages)


@pytest.mark.parametrize("gate", ["revoked", "online_off"])
def test_root_nested_aliases_and_both_progress_routes_fail_closed(ready_site, gate):
    site = ready_site
    actor = Actor(1, "release-policy")
    if gate == "online_off":
        products = ProductService(site.db)
        product = products.get_product(1)
        revised = products.update(actor, product.id, product.revision,
            product.data.model_copy(update={"policy": product.data.policy.model_copy(update={"online": False})}))
        products.activate(actor, revised.id, revised.revision,
            SalesChecklist(quality=True, sources=True, ai=True, mobile=True, downloads=True))
    with httpx.Client(base_url=site.url, verify=False, trust_env=False, timeout=10) as owner, httpx.Client(base_url=site.url, verify=False, trust_env=False, timeout=10) as buyer:
        sale_receipt(owner, buyer, site)
        if gate == "revoked":
            assert socket_post(owner, site, "/admin/entitlements/1/revoke", revision="1", reason="Verified revocation",
                               idempotency_key="revoke", confirm="on").status_code == 303
        before = snapshot(site.db)
        for path in ("index.html", "chapters/index.html", "chapters/1", "chapters/intro.html", "lessons/unit/intro.html", "chapters/02.html", "assets/free.png"):
            assert buyer.get("/learn/fixture-course/" + path).status_code == 403
        token = buyer.cookies.get("course_csrf_fixture-course")
        assert buyer.post("/api/progress", json=dict(course_slug="fixture-course", chapter_number=1, completed=True, csrf_token=token),
                           headers={"Origin": site.origin}).status_code == 403
        assert buyer.post("/learn/fixture-course/chapters/1/progress", data=dict(completed="true", csrf_token=token),
                           headers={"Origin": site.origin}).status_code == 403
        assert snapshot(site.db) == before


def test_matched_copied_database_and_content_restore_reads_relocated_bytes(ready_site, tmp_path):
    site = ready_site
    with httpx.Client(base_url=site.url, verify=False, trust_env=False, timeout=10) as owner, httpx.Client(base_url=site.url, verify=False, trust_env=False, timeout=10) as buyer:
        _, key = sale_receipt(owner, buyer, site)
        assert buyer.post("/learn/fixture-course/chapters/1/progress", data=dict(completed="true",
            csrf_token=buyer.cookies.get("course_csrf_fixture-course")), headers={"Origin": site.origin}).status_code == 303
    restored = tmp_path / "restored"
    content = restored / "content"
    shutil.copytree(site.content / "fixture-course", content / "fixture-course")
    backup = restored / "matched.db"
    backup_database(site.db, backup)
    all_tables = ("schema_migrations", "courses", "chapters", "products", "orders", "code_batches", "access_codes",
                  "entitlements", "sessions", "recovery_credentials", "entitlement_progress", "legacy_code_origins", "legacy_entitlement_origins", "legacy_verifications")
    before = snapshot(backup, all_tables)
    copied = content / "fixture-course"
    from course_platform.content_inspection import inspect_package
    from course_platform.content import load_course_package
    inspection = inspect_package(copied)
    manifest = load_course_package(copied).manifest
    with closing(open_readonly(backup)) as c:
        row = c.execute("SELECT course_id,slug,version,package_hash FROM courses").fetchone()
        assert tuple(row) == (manifest.course_id, manifest.slug, manifest.version, inspection.fingerprint)
        chapters = [tuple(r) for r in c.execute("SELECT chapter_number,title,path,free_preview FROM chapters ORDER BY chapter_number")]
        assert chapters == sorted((ch.number, ch.title, ch.path, int(ch.free_preview)) for ch in manifest.chapters)
    # Consume the actual latest5 operator example, not an equivalent private recipe.
    runbook = (Path(__file__).resolve().parents[1] / "docs/operations/admin-v1-runbook.md").read_text(encoding="utf8")
    section = runbook.split("### latest5", 1)[1].split("### pre-v5", 1)[0]
    code = re.search(r"```python\n(.*?)```", section, re.S).group(1)
    for label, value in {"<副本DB绝对路径>": backup, "<副本内容根绝对路径>": content,
                         "<该课程目录>": "fixture-course"}.items():
        code = code.replace(label, str(value).replace("\\", "/"))
    exec(compile(code, "runbook-latest5-relocation", "exec"), {})
    after = snapshot(backup, all_tables)
    assert {k:v for k,v in after.items() if k != "courses"} == {k:v for k,v in before.items() if k != "courses"}
    assert check_database(backup)["foreign_keys"] == "ok" and check_database(backup)["version"] == 5
    assert site.content.exists() and copied.resolve() != site.content.resolve()
    # Original bytes become unavailable while the original directory still exists.
    original_chapter = site.content / "fixture-course" / "lessons/unit/intro.html"
    original_bytes = original_chapter.read_bytes()
    original_chapter.write_text("ORIGINAL MUST NOT BE READ", encoding="utf8")
    app = create_app(Settings("", "", content, backup, 72, "test", "http://testserver"))
    try:
        with TestClient(app, client=("198.51.100.8", 50001), follow_redirects=False) as client:
            assert credential_post(client, "/access/restore", credential=key).status_code == 200
            page = client.get("/learn/fixture-course/lessons/unit/intro.html")
            assert page.status_code == 200 and "Matched restored lesson bytes" in page.text
            assert "ORIGINAL MUST NOT BE READ" not in page.text
            assert "取消完成" in page.text
            copied_chapter = copied / "lessons/unit/intro.html"
            copied_bytes = copied_chapter.read_bytes()
            copied_chapter.write_text("MISMATCHED COPY", encoding="utf8")
            assert client.get("/learn/fixture-course/chapters/1").status_code == 503
            copied_chapter.write_bytes(copied_bytes)
            assert client.get("/learn/fixture-course/chapters/1").status_code == 200
    finally:
        original_chapter.write_bytes(original_bytes)
    assert snapshot(site.db, all_tables) == before
    # The runtime proof may add sessions; original promises, progress and identities do not change.
    final = snapshot(backup, all_tables)
    for table in all_tables:
        if table not in ("courses", "sessions"):
            assert final[table] == before[table]
    print("Matched latest5 backup/content restore: FK, identity/hash, promises/deadlines/progress preserved; relocated bytes read; mismatched bytes denied")


def test_http_reset_fault_replay_and_competing_restores(delivery_client, actor):
    client = delivery_client
    app = client.app
    from course_platform.operations.orders import OrderInput
    order = app.state.order_service.record(actor, OrderInput(channel="shop", shop_id="shop", external_order_id="http-atomic",
        product_id=1, paid_cents=100, paid_at=app.state.order_service.clock(), note="Verified payment"), "atomic-order")
    issued = app.state.order_service.issue(actor, order.id, 1, "atomic-issue")
    response = redeem(client, issued.codes[0].raw_code)
    old_key = re.search(r"LK-[A-Za-z0-9_-]{43}", response.text).group()
    login(client)
    db = app.state.settings.database_path
    before = snapshot(db, ("entitlements", "sessions", "recovery_credentials", "operation_requests", "entitlement_progress"))
    with transaction(db) as c:
        c.execute("CREATE TRIGGER release_reset_fault BEFORE INSERT ON recovery_credentials BEGIN SELECT RAISE(ABORT,'private acceptance fault'); END")
    data = dict(revision="1", reason="Verified order holder", idempotency_key="reset-fault", confirm="on")
    assert post(client, "/admin/entitlements/1/reset-credential", data).status_code >= 400
    assert snapshot(db, tuple(before)) == before
    with transaction(db) as c:
        c.execute("DROP TRIGGER release_reset_fault")
    result = post(client, "/admin/entitlements/1/reset-credential", data)
    assert result.status_code == 200
    key = re.search(r"LK-[A-Za-z0-9_-]{43}", result.text).group()
    replay = post(client, "/admin/entitlements/1/reset-credential", data)
    assert replay.status_code == 200 and key not in replay.text
    assert credential_post(client, "/access/restore", credential=old_key).status_code == 403
    def restore(_):
        with TestClient(app, client=("198.51.100.8", 50001), follow_redirects=False) as device:
            return credential_post(device, "/access/restore", credential=key).status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(restore, range(4)))
    assert sorted(statuses) == [200, 200, 200, 409]
    with closing(open_readonly(db)) as c:
        assert c.execute("SELECT count(*) FROM sessions WHERE revoked_at IS NULL").fetchone()[0] == 3


def test_offline_old_database_restore_preserves_independent_unavailable_history(original_restore_history, tmp_path, clock, monkeypatch):
    from course_platform.database import migrate_database
    from course_platform.content_inspection import inspect_package
    from course_platform.content import load_course_package
    from frozen_original_access import AccessService as OriginalAccess
    original = original_restore_history

    # A genuine old producer also owned an unavailable course. No verification,
    # policy, code/session association or no-fixed-expiry is fabricated for it.
    with transaction(original.source) as c:
        c.execute("INSERT INTO courses VALUES ('missing','missing-course','Unavailable','AI','0.1.0','published','missing-original','2026-10-02T00:00:00+00:00')")
        c.execute("INSERT INTO chapters VALUES ('missing',1,'Unavailable','chapters/01.html',0)")
    old = OriginalAccess(original.source)
    unavailable_code = old.create_access_code("missing")
    unavailable_session = old.redeem_access_code(unavailable_code, "missing")
    old.record_progress(unavailable_session.session_id, "missing", 1, True)
    source_before = snapshot(original.source, ("courses", "chapters", "access_codes", "sessions", "progress", "events"))
    restored = tmp_path / "old-restore"
    copied = restored / "content" / "fixture-course"
    shutil.copytree(original.package, copied)
    assert inspect_package(copied).fingerprint == inspect_package(original.package).fingerprint
    manifest = load_course_package(copied).manifest
    assert (manifest.course_id, manifest.slug, manifest.version) == (original.course, "fixture-course", "0.1.0")
    db = restored / "restore.db"
    backup_database(original.source, db)
    compatible = restored / "old-compatible.db"
    backup_database(db, compatible)
    # Explicit copied-only relocation before offline conversion, so inspection
    # cannot read the original source paths during migration.
    with transaction(db) as c:
        c.execute("UPDATE courses SET content_path=? WHERE course_id=?", (str(copied.resolve()), original.course))
        c.execute("UPDATE courses SET content_path=? WHERE course_id='missing'", (str((restored/'content'/'unavailable').resolve()),))
    migrated = migrate_database(db, backup_path=restored / "before-v5.db")
    assert migrated.to_version == 5 and check_database(db)["foreign_keys"] == "ok"
    assert snapshot(compatible, tuple(source_before)) == source_before
    with closing(open_readonly(db)) as c:
        for old_session in source_before["sessions"]:
            row = c.execute("SELECT session_hash,course_id,created_at,expires_at FROM sessions WHERE session_hash=?", (old_session[0],)).fetchone()
            assert tuple(row) == old_session
        assert [tuple(row) for row in c.execute("SELECT * FROM progress ORDER BY rowid")] == source_before["progress"]
        assert c.execute("SELECT count(*) FROM entitlements").fetchone()[0] == len(source_before["sessions"])
        assert c.execute("SELECT count(*) FROM entitlements WHERE source_code_id IS NOT NULL OR issued_policy_json IS NOT NULL OR verified_at IS NOT NULL").fetchone()[0] == 0
        assert tuple(c.execute("SELECT content_available,package_hash FROM legacy_entitlement_origins WHERE entitlement_id=(SELECT id FROM entitlements WHERE course_id='missing')").fetchone()) == (0, None)
    monkeypatch.setattr("course_platform.app.utc_now", clock.now)
    original_bytes = (original.package / "chapters/01.html").read_bytes()
    (original.package / "chapters/01.html").write_text("UNRELATED ORIGINAL BYTES", encoding="utf8")
    try:
        app = create_app(Settings("", "", restored / "content", db, 72, "test", "http://testserver"))
        with TestClient(app, client=("198.51.100.8", 50001), follow_redirects=False) as client:
            client.cookies.set("course_session_fixture-course", original.sessions[0].session_id)
            page = client.get("/learn/fixture-course/chapters/1")
            assert page.status_code == 200 and "UNRELATED ORIGINAL BYTES" not in page.text
            assert "取消完成" in page.text
            client.cookies.set("course_session_missing-course", unavailable_session.session_id)
            assert client.get("/learn/missing-course/chapters/1").status_code == 503
    finally:
        (original.package / "chapters/01.html").write_bytes(original_bytes)
    assert snapshot(original.source, tuple(source_before)) == source_before
    assert snapshot(compatible, tuple(source_before)) == source_before
    print("Actual old6-table copied DB/content -> offline latest5 restore passed; old-compatible rollback copy unchanged; unavailable/unknown independent history retained")
