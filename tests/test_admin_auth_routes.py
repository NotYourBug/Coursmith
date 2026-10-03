"""Real HTTP, cookies, CSRF and owner CLI boundaries."""

import re
import asyncio
import hashlib
import json
from dataclasses import replace
from threading import Event, Thread

import httpx
import pytest
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient

from course_platform import cli
from course_platform.admin.routes.auth import AdminRoute, require_admin_post, router
from course_platform.database import transaction
from course_platform.domain import BusinessError


PASSWORD = "example-pass-123"


def field(response, name):
    match = re.search(r'name="' + name + r'"[^>]*value="([^"]*)"', response.text)
    assert match, response.text
    return match.group(1)


def sign_in(client, password=PASSWORD):
    page = client.get("/admin/login")
    return client.post("/admin/login", data={"username": "owner", "password": password,
                       "csrf_token": field(page, "csrf_token")}, headers={"Origin": str(client.base_url).rstrip("/")})


def test_admin_routes_reject_anonymous_and_buyer_cookie(admin_client, buyer_client, admin_service, clock):
    admin_service.initialize_owner("owner", PASSWORD)
    for client in (admin_client, buyer_client):
        client.cookies.set("course_session_example", "buyer-token")
        response = client.get("/admin/account/password")
        assert response.status_code == 303 and response.headers["location"] == "/admin/login"
        assert client.post("/admin/logout", data={}).status_code == 401
        assert client.post("/admin/account/password", data={}).status_code == 401
    assert sign_in(admin_client).status_code == 303
    assert not buyer_client.cookies.get("coursmith_admin")
    page = admin_client.get("/admin/account/password")
    token = admin_client.cookies.get("coursmith_admin")
    response = admin_client.post("/admin/logout", data={"csrf_token": field(page, "csrf_token"),
                                 "revision": field(page, "revision")}, headers={"Origin": "http://testserver"})
    assert response.status_code == 303
    admin_client.cookies.set("coursmith_admin", token, path="/admin")
    assert admin_client.get("/admin/account/password").status_code == 303
    assert sign_in(admin_client).status_code == 303
    clock.advance(minutes=30)
    assert admin_client.get("/admin/account/password").status_code == 303
    assert admin_client.post("/admin/logout", data={}).status_code == 401


def test_uninitialized_admin_responses_are_503_no_store(admin_client):
    for path in ("/admin", "/admin/login", "/admin/account/password", "/admin/logout"):
        response = admin_client.post(path, data={}) if path == "/admin/logout" else admin_client.get(path)
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
        assert "init-admin" in response.text


@pytest.mark.parametrize("origin,csrf", [(None, True), ("http://evil.test", True), ("http://testserver", False)])
def test_login_pre_csrf_and_origin_are_required(admin_client, admin_service, db_path, origin, csrf):
    admin_service.initialize_owner("owner", PASSWORD)
    page = admin_client.get("/admin/login")
    data = {"username": "owner", "password": PASSWORD}
    if csrf:
        data["csrf_token"] = field(page, "csrf_token")
    response = admin_client.post("/admin/login", data=data, headers={"Origin": origin} if origin else {})
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    with transaction(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM admin_sessions").fetchone()[0] == 0


def test_failed_login_renews_one_shot_challenge_and_retains_only_username(admin_client, admin_service):
    admin_service.initialize_owner("owner", PASSWORD)
    page = admin_client.get("/admin/login")
    old = field(page, "csrf_token")
    response = admin_client.post("/admin/login", data={"username": "<owner>", "password": "secret-wrong",
                                 "csrf_token": old}, headers={"Origin": "http://testserver"})
    assert response.status_code == 401
    renewed = field(response, "csrf_token")
    assert renewed != old
    assert "&lt;owner&gt;" in response.text and "secret-wrong" not in response.text
    assert admin_client.post("/admin/login", data={"username": "owner", "password": PASSWORD,
                            "csrf_token": old}, headers={"Origin": "http://testserver"}).status_code == 403
    page = admin_client.get("/admin/login")
    assert admin_client.post("/admin/login", data={"username": "owner", "password": PASSWORD,
                            "csrf_token": field(page, "csrf_token")}, headers={"Origin": "http://testserver"}).status_code == 303


def test_login_challenge_expires_at_ten_minutes(admin_client, admin_service, clock):
    admin_service.initialize_owner("owner", PASSWORD)
    page = admin_client.get("/admin/login")
    clock.advance(minutes=10)
    assert admin_client.post("/admin/login", data={"username": "owner", "password": PASSWORD,
                            "csrf_token": field(page, "csrf_token")}, headers={"Origin": "http://testserver"}).status_code == 403


def test_login_rotates_token_and_production_cookie_is_secure(http_app_factory, admin_settings,
                                                            admin_service, csrf_service, rate_limiter):
    admin_service.initialize_owner("owner", PASSWORD)
    settings = replace(admin_settings, environment="production", site_origin="https://courses.example")
    app = http_app_factory([router], settings, {"admin_service": admin_service, "csrf_service": csrf_service,
                                              "rate_limiter": rate_limiter})
    with TestClient(app, base_url=settings.site_origin, client=("198.51.100.7", 50000), follow_redirects=False) as client:
        first = sign_in(client)
        original = client.cookies.get("coursmith_admin")
        cookies = first.headers.get_list("set-cookie")
        for name in ("coursmith_admin", "coursmith_admin_csrf"):
            cookie = next(value for value in cookies if value.startswith(name + "="))
            assert "Path=/admin" in cookie and "SameSite=strict" in cookie
            assert "Secure" in cookie and "HttpOnly" in cookie
        assert sign_in(client, "wrong-password").status_code == 401
        assert admin_service.require_session(original, request_id="failed-login").admin_id == 1
        assert sign_in(client).status_code == 303
        assert client.cookies.get("coursmith_admin") != original
        with pytest.raises(BusinessError):
            admin_service.require_session(original, request_id="old")


def test_login_limit_forwards_retry_after_and_fresh_challenge(admin_client, admin_service):
    admin_service.initialize_owner("owner", PASSWORD)
    for _ in range(5):
        assert sign_in(admin_client, "wrong-password").status_code == 401
    response = sign_in(admin_client)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "900"
    assert field(response, "csrf_token") == admin_client.cookies.get("coursmith_admin_login_csrf")
    assert PASSWORD not in response.text


@pytest.mark.parametrize("fault,status", [("origin", 403), ("csrf", 403), ("duplicate", 400),
                                          ("oversized", 413), ("revision", 409)])
def test_admin_post_boundary_denials_do_not_change_password(admin_client, admin_service, fault, status):
    admin_service.initialize_owner("owner", PASSWORD)
    sign_in(admin_client)
    token = admin_client.cookies.get("coursmith_admin")
    page = admin_client.get("/admin/account/password")
    data = {"csrf_token": field(page, "csrf_token"), "revision": field(page, "revision"),
            "current_password": PASSWORD, "new_password": "replacement-pass", "confirm_password": "replacement-pass"}
    headers = {"Origin": "http://testserver"}
    if fault == "origin":
        headers["Origin"] = "http://evil.test"
    elif fault == "csrf":
        data["csrf_token"] = "b" * 43
    elif fault == "revision":
        data["revision"] = "0"
    elif fault == "oversized":
        data["extra"] = "x" * 65536
    if fault == "duplicate":
        response = admin_client.post("/admin/account/password", content="revision=1&revision=2",
                                     headers={**headers, "Content-Type": "application/x-www-form-urlencoded"})
    else:
        response = admin_client.post("/admin/account/password", data=data, headers=headers)
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert admin_service.require_session(token, request_id="still-valid").admin_id == 1


def test_password_route_revokes_sessions_and_clears_cookies(admin_client, admin_service):
    admin_service.initialize_owner("owner", PASSWORD)
    sign_in(admin_client)
    token = admin_client.cookies.get("coursmith_admin")
    page = admin_client.get("/admin/account/password")
    response = admin_client.post("/admin/account/password", data={"csrf_token": field(page, "csrf_token"),
                                 "revision": field(page, "revision"), "current_password": PASSWORD,
                                 "new_password": "replacement-pass", "confirm_password": "replacement-pass"},
                                 headers={"Origin": "http://testserver"})
    assert response.status_code == 303
    assert admin_client.cookies.get("coursmith_admin") is None
    assert admin_client.cookies.get("coursmith_admin_csrf") is None
    with pytest.raises(BusinessError):
        admin_service.require_session(token, request_id="old")
    assert sign_in(admin_client, "replacement-pass").status_code == 303


def test_admin_shell_has_working_navigation_and_safe_correlated_errors(admin_client, admin_service):
    admin_service.initialize_owner("owner", PASSWORD)
    sign_in(admin_client)
    response = admin_client.get("/admin")
    assert response.status_code == 200
    assert 'href="/admin/account/password"' in response.text
    assert "后续阶段" in response.text
    assert "<script" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "script-src http://testserver/static/admin/;" in response.headers["content-security-policy"]
    page = admin_client.get("/admin/account/password")
    response = admin_client.post("/admin/account/password", data={"csrf_token": field(page, "csrf_token"),
                                 "revision": field(page, "revision"), "current_password": "wrong-secret",
                                 "new_password": "replacement-pass", "confirm_password": "replacement-pass"},
                                 headers={"Origin": "http://testserver", "X-Request-ID": "untrusted-secret"})
    assert response.status_code == 401
    assert response.headers["x-request-id"] in response.text
    assert "untrusted-secret" not in response.text and "wrong-secret" not in response.text


def test_init_admin_password_never_appears_in_argv_or_output(monkeypatch, capsys, admin_settings, db_path):
    monkeypatch.setattr(cli, "load_settings", lambda: admin_settings)
    monkeypatch.setattr("builtins.input", lambda prompt: "Owner")
    prompts = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: next(prompts))
    argv = ["init-admin"]
    assert cli.main(argv) == 0
    assert argv == ["init-admin"]
    assert PASSWORD not in capsys.readouterr().out
    with transaction(db_path) as connection:
        assert connection.execute("SELECT username FROM admins").fetchone()[0] == "owner"
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["init-admin", "--password", PASSWORD])


def test_init_admin_mismatch_does_not_initialize(monkeypatch, capsys, admin_settings, db_path):
    monkeypatch.setattr(cli, "load_settings", lambda: admin_settings)
    monkeypatch.setattr("builtins.input", lambda prompt: "owner")
    prompts = iter([PASSWORD, "different-secret"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: next(prompts))
    assert cli.main(["init-admin"]) == 1
    output = capsys.readouterr().out
    assert PASSWORD not in output and "different-secret" not in output
    with transaction(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM admins").fetchone()[0] == 0


def test_cli_actor_requires_credentials(monkeypatch, admin_service):
    admin_service.initialize_owner("owner", PASSWORD)
    monkeypatch.setattr("builtins.input", lambda prompt: "owner")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "wrong-secret")
    with pytest.raises(BusinessError):
        cli.authenticate_owner(admin_service)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: PASSWORD)
    assert cli.authenticate_owner(admin_service).admin_id == 1


def test_shared_admin_post_leaves_object_revision_to_its_domain(admin_app, admin_client, admin_service):
    # A domain revision of 7 must not be compared with the owner's revision 1.
    boundary = APIRouter(route_class=AdminRoute)

    @boundary.post("/admin/boundary-test")
    async def check(request: Request):
        actor, form = await require_admin_post(request)
        return {"admin_id": actor.admin_id, "revision": form["revision"]}

    admin_app.include_router(boundary)
    admin_service.initialize_owner("owner", PASSWORD)
    sign_in(admin_client)
    page = admin_client.get("/admin/account/password")
    response = admin_client.post("/admin/boundary-test", data={"csrf_token": field(page, "csrf_token"),
                                 "revision": "7"}, headers={"Origin": "http://testserver"})
    assert response.status_code == 200
    assert response.json() == {"admin_id": 1, "revision": "7"}


def test_password_failure_keeps_a_usable_form_without_secrets(admin_client, admin_service):
    admin_service.initialize_owner("owner", PASSWORD)
    sign_in(admin_client)
    page = admin_client.get("/admin/account/password")
    response = admin_client.post("/admin/account/password", data={"csrf_token": field(page, "csrf_token"),
                                 "revision": field(page, "revision"), "current_password": PASSWORD,
                                 "new_password": "replacement-secret", "confirm_password": "different-secret"},
                                 headers={"Origin": "http://testserver"})
    assert response.status_code == 400
    assert 'action="/admin/account/password"' in response.text
    assert field(response, "revision") == "1"
    assert response.headers["x-request-id"] in response.text
    for secret in (PASSWORD, "replacement-secret", "different-secret"):
        assert secret not in response.text


def test_unsupported_cli_password_argument_is_not_echoed(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["init-admin", "--password", "argv-secret"])
    assert "argv-secret" not in capsys.readouterr().err


def test_admin_method_rejections_are_private_and_correlated(admin_client):
    for method, path in [("get", "/admin/logout"), ("delete", "/admin/login"),
                         ("put", "/admin/account/password")]:
        response = getattr(admin_client, method)(path)
        assert response.status_code == 405
        assert response.headers.get("cache-control") == "no-store"
        assert response.headers["x-request-id"] in response.text
        assert response.headers["allow"]


@pytest.mark.parametrize("path,fault,status,code", [
    (path, fault, status, code)
    for path in ("/admin/login", "/admin/logout", "/admin/account/password")
    for fault, status, code in [("origin", 403, "invalid_origin"),
                               ("oversized", 413, "body_too_large"),
                               ("media", 415, "unsupported_media_type"),
                               ("form", 400, "invalid_form"), ("csrf", 403, "invalid_csrf")]
] + [
    ("/admin/login", "password", 401, "invalid_credentials"),
    ("/admin/login", "lockout", 429, "rate_limited"),
    ("/admin/logout", "malformed_revision", 409, "stale_revision"),
    ("/admin/logout", "stale_revision", 409, "stale_revision"),
    ("/admin/account/password", "malformed_revision", 409, "stale_revision"),
    ("/admin/account/password", "stale_revision", 409, "stale_revision"),
    ("/admin/account/password", "confirmation", 400, "password_mismatch"),
    ("/admin/account/password", "password", 401, "invalid_credentials"),
])
def test_rejected_auth_post_has_one_safe_correlated_audit(
        admin_client, admin_service, db_path, path, fault, status, code):
    # Removing route denial recording loses boundary errors; omitting the
    # service marker duplicates audits for credential/CSRF/rate denials.
    admin_service.initialize_owner("owner", PASSWORD)
    assert sign_in(admin_client).status_code == 303
    old_token = admin_client.cookies.get("coursmith_admin")
    page = admin_client.get("/admin/login" if path == "/admin/login" else "/admin/account/password")
    csrf = field(page, "csrf_token")
    data = {"csrf_token": csrf, "revision": "1", "username": "owner", "password": PASSWORD,
            "current_password": PASSWORD, "new_password": "replacement-secret",
            "confirm_password": "replacement-secret"}
    headers = {"Origin": "http://testserver", "X-Request-ID": "client-secret-id"}
    if fault == "lockout":
        for _ in range(5):
            admin_service.rate_limiter.record_login_failure("owner", "independent-source")
    with transaction(db_path) as connection:
        last_id = connection.execute("SELECT max(id) FROM admin_events").fetchone()[0]
    if fault == "origin":
        headers["Origin"] = "http://evil.test/secret-origin"
    elif fault == "oversized":
        data["secret-extra"] = "x" * 65536
    elif fault == "csrf":
        data["csrf_token"] = "b" * 43
    elif fault == "malformed_revision":
        data["revision"] = "secret-revision"
    elif fault == "stale_revision":
        data["revision"] = "0"
    elif fault == "confirmation":
        data["confirm_password"] = "different-secret"
    elif fault == "password":
        data["password"] = data["current_password"] = "incorrect-secret"
    if fault in ("form", "media"):
        response = admin_client.post(path, content="password=secret-wire&password=secret-wire",
            headers={**headers, "Content-Type": "text/plain" if fault == "media"
                     else "application/x-www-form-urlencoded"})
    else:
        response = admin_client.post(path, data=data, headers=headers)
    assert response.status_code == status
    with transaction(db_path) as connection:
        events = [dict(row) for row in connection.execute("SELECT * FROM admin_events WHERE id>?", (last_id,))]
        assert connection.execute("SELECT revision FROM admins").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM admin_sessions WHERE revoked_at IS NULL").fetchone()[0] == 1
    assert len(events) == 1
    event = events[0]
    action = {"/admin/login": "auth.login", "/admin/logout": "auth.logout",
              "/admin/account/password": "admin.password_change"}[path]
    assert (event["action"], event["outcome"], event["reason"]) == (action, "denied", code)
    assert json.loads(event["changes_json"]) == {"error_code": code}
    assert event["request_id"] == response.headers["x-request-id"]
    assert re.fullmatch(r"[0-9a-f]{32}", event["request_id"])
    for secret in (PASSWORD, old_token, csrf, "b" * 43, "replacement-secret", "incorrect-secret",
                   "different-secret", "client-secret-id", "secret-wire", "secret-revision"):
        assert secret not in str(events)
        assert hashlib.sha256(secret.encode()).hexdigest() not in str(events)
    assert admin_service.require_session(old_token, request_id="retained").admin_id == 1


@pytest.mark.parametrize("operation", ["login_verify", "password_verify", "password_hash",
                                      "login_consume", "login_retry", "logout_session",
                                      "password_retry_session"])
def test_auth_work_does_not_block_unrelated_requests(
        admin_app, admin_client, admin_service, csrf_service, monkeypatch, operation):
    # A gate delays the real synchronous operation without replacing its
    # outcome or persistence. Direct invocation in an async handler prevents
    # our unrelated request from completing before the watchdog releases it.
    from course_platform.admin import auth

    admin_service.initialize_owner("owner", PASSWORD)
    assert sign_in(admin_client).status_code == 303
    path = "/admin/login" if operation.startswith("login") else "/admin/account/password"
    if operation == "logout_session":
        path = "/admin/logout"
    page = admin_client.get("/admin/login" if path == "/admin/login" else "/admin/account/password")
    data = {"csrf_token": field(page, "csrf_token"), "revision": "1", "username": "owner",
            "password": PASSWORD, "current_password": PASSWORD,
            "new_password": "replacement-pass", "confirm_password": "replacement-pass"}
    headers = {"Origin": "http://evil.test" if operation == "login_retry" else "http://testserver"}
    if operation == "password_retry_session":
        data["confirm_password"] = "different-secret"
    entered, release, progressed = Event(), Event(), Event()
    calls = 0
    target_call = 2 if operation == "password_retry_session" else 1
    if operation.endswith("verify"):
        target, name = auth, "_matches"
    elif operation == "password_hash":
        target, name = auth._PASSWORDS, "hash"
    elif operation == "login_consume":
        target, name = csrf_service, "consume_challenge"
    elif operation == "login_retry":
        target, name = csrf_service, "issue_challenge"
    else:
        target, name = admin_service, "require_session"
    real_operation = getattr(target, name)

    def gated(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == target_call:
            entered.set()
            assert release.wait(5), "watchdog did not release real auth work"
        return real_operation(*args, **kwargs)

    monkeypatch.setattr(target, name, gated)

    @admin_app.get("/unrelated")
    async def unrelated():
        return {"available": True}

    def watchdog():
        if entered.wait(5):
            progressed.wait(1)
        release.set()

    watcher = Thread(target=watchdog)
    watcher.start()

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admin_app),
                base_url="http://testserver", cookies=dict(admin_client.cookies.items())) as client:
            pending = asyncio.create_task(client.post(path, data=data, headers=headers))
            try:
                reached = await asyncio.to_thread(entered.wait, 5)
                probe = await client.get("/unrelated")
                before_release = not release.is_set()
                progressed.set()
            finally:
                release.set()
            response = await pending
            assert reached
            assert probe.status_code == 200 and probe.json() == {"available": True}
            assert before_release, "auth work prevented unrelated request progress"
            assert response.status_code == (403 if operation == "login_retry" else
                                            400 if operation == "password_retry_session" else 303)

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        watcher.join(timeout=5)


@pytest.mark.parametrize("path", ["/admin/login", "/admin/logout", "/admin/account/password"])
def test_real_sqlite_wait_does_not_block_unrelated_requests(admin_app, admin_client, admin_service, db_path, path):
    admin_service.initialize_owner("owner", PASSWORD)
    assert sign_in(admin_client).status_code == 303
    page = admin_client.get("/admin/login" if path == "/admin/login" else "/admin/account/password")
    data = {"csrf_token": field(page, "csrf_token"), "revision": "1", "username": "owner",
            "password": PASSWORD, "current_password": PASSWORD,
            "new_password": "replacement-pass", "confirm_password": "replacement-pass"}
    locked, release, progressed = Event(), Event(), Event()

    @admin_app.get("/unrelated")
    async def unrelated():
        return {"available": True}

    def hold_writer():
        # Independent real SQLite connection: requests must actually wait for
        # this RESERVED lock, while the same ASGI loop serves another request.
        with transaction(db_path, immediate=True):
            locked.set()
            progressed.wait(1)
            release.set()

    writer = Thread(target=hold_writer)
    writer.start()
    assert locked.wait(5)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admin_app),
                base_url="http://testserver", cookies=dict(admin_client.cookies.items())) as client:
            pending = asyncio.create_task(client.post(path, data=data, headers={"Origin": "http://testserver"}))
            try:
                await asyncio.sleep(0.05)
                probe = await client.get("/unrelated")
                before_release = not release.is_set() and not pending.done()
            finally:
                progressed.set()
            response = await pending
            assert probe.status_code == 200
            assert before_release, "SQLite lock wait blocked the ASGI loop"
            assert response.status_code == 303

    try:
        asyncio.run(exercise())
    finally:
        progressed.set()
        writer.join(timeout=5)
