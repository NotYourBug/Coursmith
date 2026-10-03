"""Real owner HTTP boundaries for product and category administration."""

import asyncio
import json
import re
from threading import Event, Thread

import httpx
import pytest

from course_platform.database import transaction
from course_platform.admin.routes.products import router  # noqa: F401


def login(client):
    page = client.get("/admin/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    assert client.post("/admin/login", data={"username": "owner", "password": "example-pass-123",
        "csrf_token": csrf}, headers={"Origin": "http://testserver"}).status_code == 303


def post(client, path, data):
    return client.post(path, data={"csrf_token": client.cookies.get("coursmith_admin_csrf"), **data},
                       headers={"Origin": "http://testserver"})


def form_data(data):
    values = data.model_dump(exclude={"policy", "channels", "outcomes"})
    return {**{key: value or "" for key, value in values.items()}, "outcomes": "\n".join(data.outcomes),
        "channels": "\n".join(f"{channel.name}|{channel.url}" for channel in data.channels),
        "access_mode": data.policy.access_mode, "access_days": str(data.policy.access_days or ""),
        "online": "on", "update_policy": "current_version"}


def test_product_pages_require_real_owner(product_client, product_owner):
    product_client.cookies.set("course_session_fixture-course", "buyer-token")
    for path in ("/admin/products", "/admin/products/new", "/admin/products/1", "/admin/categories"):
        response = product_client.get(path)
        assert response.status_code == 303 and response.headers["location"] == "/admin/login"
        assert response.headers["cache-control"] == "no-store"
    assert post(product_client, "/admin/products/new", {}).status_code == 401
    login(product_client)
    assert product_client.get("/admin/products").status_code == 200


def test_product_create_edit_activate_pause_archive_and_category_forms(
        product_client, product_owner, published_product_data, product_service):
    login(product_client)
    shell = product_client.get("/admin")
    assert 'href="http://testserver/admin/products"' in shell.text and 'href="http://testserver/admin/categories"' in shell.text
    assert product_client.get("/admin/products/new").status_code == 200
    created = post(product_client, "/admin/products/new", form_data(published_product_data))
    assert created.status_code == 303 and created.headers["location"] == "/admin/products/1"
    page = product_client.get("/admin/products/1")
    assert page.status_code == 200 and "学习期限" in page.text and "领取期限" in page.text
    draft = product_service.get_product(1)
    changed = form_data(draft.data) | {"title": "修改标题", "revision": str(draft.revision)}
    assert post(product_client, "/admin/products/1", changed).status_code == 303
    assert post(product_client, "/admin/products/1", changed).status_code == 409
    draft = product_service.get_product(1)
    activation = {"revision": str(draft.revision), **{key: "on" for key in ("quality", "sources", "ai", "mobile", "downloads")}}
    assert post(product_client, "/admin/products/1/activate", activation).status_code == 303
    active = product_service.get_product(1)
    assert post(product_client, "/admin/products/1/pause", {"revision": str(active.revision)}).status_code == 303
    paused = product_service.get_product(1)
    assert post(product_client, "/admin/products/1/activate", {"revision": str(paused.revision)}).status_code == 409
    assert post(product_client, "/admin/products/1/archive", {"revision": str(paused.revision)}).status_code == 303
    assert product_service.get_product(1).status == "archived"
    assert post(product_client, "/admin/categories", {"slug": "second", "name": "第二类", "sort_order": "5", "enabled": "on"}).status_code == 303
    assert post(product_client, "/admin/categories/2", {"revision": "1", "slug": "second", "name": "重命名", "sort_order": "-2"}).status_code == 303
    assert post(product_client, "/admin/categories/2", {"revision": "1", "slug": "second", "name": "旧", "sort_order": "0"}).status_code == 409
    page = product_client.get("/admin/categories")
    assert "重命名" in page.text


def test_markup_and_javascript_shop_urls_are_not_executable(
        product_client, product_owner, published_product_data, product_service):
    login(product_client)
    values = form_data(published_product_data) | {"title": '<script>alert("private")</script>',
        "support_text": "<img src=x onerror=steal()>", "channels": "店铺|javascript:steal()"}
    assert post(product_client, "/admin/products/new", values).status_code == 400
    values["channels"] = '店铺|https://shop.example/?q="onclick=steal()'
    response = post(product_client, "/admin/products/new", values)
    assert response.status_code == 303
    page = product_client.get(response.headers["location"])
    assert "<script>" not in page.text and "<img src=x" not in page.text
    assert "&lt;script&gt;" in page.text and "&lt;img" in page.text
    assert 'href="javascript:' not in page.text
    assert page.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in page.headers["content-security-policy"]


def test_detail_reports_failure_without_changing_business_state(
        product_client, product_owner, active_product, product_service, db_path, fixture_package):
    login(product_client)
    (fixture_package / "LICENSE.txt").unlink()
    with transaction(db_path) as connection:
        before = tuple(connection.execute("SELECT * FROM products").fetchone())
    page = product_client.get(f"/admin/products/{active_product.id}")
    assert page.status_code == 200 and "Course package is unavailable" in page.text
    with transaction(db_path) as connection:
        assert tuple(connection.execute("SELECT * FROM products").fetchone()) == before


@pytest.mark.parametrize("path,action", [("/admin/products/new", "product.create"),
    ("/admin/products/1", "product.update"), ("/admin/products/1/activate", "product.sales_check"),
    ("/admin/products/1/pause", "product.update"), ("/admin/products/1/archive", "product.update"),
    ("/admin/categories", "category.create"), ("/admin/categories/1", "category.update")])
@pytest.mark.parametrize("fault,status,code", [("origin", 403, "invalid_origin"),
    ("csrf", 403, "invalid_csrf"), ("body", 413, "body_too_large"),
    ("duplicate", 400, "invalid_form"), ("media", 415, "unsupported_media_type")])
def test_boundary_denial_has_one_safe_server_correlated_audit(
        product_client, product_owner, active_product, db_path, path, action, fault, status, code):
    login(product_client)
    csrf = product_client.cookies.get("coursmith_admin_csrf")
    data = {"revision": "1", "csrf_token": csrf, "title": "secret-title"}
    headers = {"Origin": "http://testserver", "X-Request-ID": "untrusted-secret-id"}
    if fault == "origin":
        headers["Origin"] = "https://evil.example/private"
    elif fault == "csrf":
        data["csrf_token"] = "b" * 43
    elif fault == "body":
        data["title"] = "x" * 65536
    with transaction(db_path) as connection:
        last = connection.execute("SELECT max(id) FROM admin_events").fetchone()[0]
        before = tuple(connection.execute("SELECT * FROM products").fetchone())
    if fault in ("duplicate", "media"):
        response = product_client.post(path, content="title=secret&title=secret", headers={**headers,
            "Content-Type": "text/plain" if fault == "media" else "application/x-www-form-urlencoded"})
    else:
        response = product_client.post(path, data=data, headers=headers)
    assert response.status_code == status
    with transaction(db_path) as connection:
        events = [dict(row) for row in connection.execute("SELECT * FROM admin_events WHERE id>?", (last,))]
        assert tuple(connection.execute("SELECT * FROM products").fetchone()) == before
    assert len(events) == 1
    assert events[0]["action"] == action and events[0]["outcome"] == "denied"
    assert json.loads(events[0]["changes_json"]) == {"error_code": code}
    assert events[0]["request_id"] == response.headers["x-request-id"]
    assert re.fullmatch("[0-9a-f]{32}", events[0]["request_id"])
    assert "secret" not in str(events) and csrf not in str(events)


def test_service_denial_not_duplicated_by_http(product_client, product_owner, active_product, db_path):
    login(product_client)
    with transaction(db_path) as connection:
        last = connection.execute("SELECT max(id) FROM admin_events").fetchone()[0]
    response = post(product_client, "/admin/products/1/pause", {"revision": "0"})
    assert response.status_code == 409
    with transaction(db_path) as connection:
        events = [dict(row) for row in connection.execute("SELECT * FROM admin_events WHERE id>?", (last,))]
    assert len(events) == 1 and events[0]["request_id"] == response.headers["x-request-id"]


def test_malformed_category_id_still_requires_owner(product_client, product_owner, db_path):
    response = post(product_client, "/admin/categories/private-identifier", {"name": "secret"})
    assert response.status_code == 401
    with transaction(db_path) as connection:
        event = dict(connection.execute("SELECT * FROM admin_events WHERE action='category.update'").fetchone())
    assert event["object_id"] == "invalid" and event["actor_admin_id"] is None
    assert "private-identifier" not in str(event)


@pytest.mark.parametrize("path", ["/admin/products/1/activate", "/admin/products/1/pause", "/admin/products/1/archive"])
def test_get_cannot_change_product_status(product_client, product_owner, active_product, product_service, path):
    login(product_client)
    response = product_client.get(path)
    assert response.status_code == 405 and response.headers["cache-control"] == "no-store"
    assert product_service.get_product(1) == active_product


@pytest.mark.parametrize("fault", ["expired", "revoked", "disabled"])
def test_invalid_owner_session_cannot_mutate_product(product_client, product_owner, active_product,
                                                    product_service, db_path, clock, fault):
    login(product_client)
    if fault == "expired":
        clock.advance(minutes=30)
    else:
        with transaction(db_path) as connection:
            connection.execute("UPDATE admin_sessions SET revoked_at='2026-10-02'" if fault == "revoked"
                               else "UPDATE admins SET enabled=0")
    response = post(product_client, "/admin/products/1/pause", {"revision": "2"})
    assert response.status_code == 401
    assert product_service.get_product(1) == active_product


def test_listing_links_preserve_escaped_filters(product_client, product_owner, published_product_data, product_service):
    for index in range(21):
        product_service.create(product_owner, published_product_data.model_copy(update={"course_id": None, "title": f"<tag> {index}"}))
    login(product_client)
    page = product_client.get("/admin/products", params={"title": "<tag>", "status": "draft", "category_id": "1"})
    assert page.status_code == 200 and "共 21 项" in page.text and "&lt;tag&gt;" in page.text
    assert "&amp;page=2" in page.text and "title=%3Ctag%3E" in page.text
    page = product_client.get("/admin/products", params={"title": "<tag>", "status": "draft", "category_id": "1", "page": "2"})
    assert page.status_code == 200 and "上一页" in page.text and "下一页" not in page.text


def test_streamed_body_is_limited_before_parsing(product_app, product_client, product_owner, active_product, db_path):
    login(product_client)
    chunks_read = []

    async def body():
        for index in range(3):
            chunks_read.append(index)
            yield b"x" * 40000

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=product_app), base_url="http://testserver",
                                     cookies=dict(product_client.cookies.items())) as client:
            return await client.post("/admin/products/1/pause", content=body(), headers={
                "Origin": "http://testserver", "Content-Type": "application/x-www-form-urlencoded"})

    response = asyncio.run(scenario())
    assert response.status_code == 413 and chunks_read == [0, 1]
    with transaction(db_path) as connection:
        assert connection.execute("SELECT status FROM products").fetchone()[0] == "active"
        assert connection.execute("SELECT count(*) FROM admin_events WHERE action='product.update' AND outcome='denied'").fetchone()[0] == 1


@pytest.mark.parametrize("days,status", [("0", 400), ("3651", 400), ("1.5", 400), ("1", 303), ("3650", 303)])
def test_http_learning_duration_boundaries(product_client, product_owner, published_product_data, days, status):
    login(product_client)
    response = post(product_client, "/admin/products/new", form_data(published_product_data) | {"access_days": days})
    assert response.status_code == status


def test_http_explicit_no_expiry_and_draft_without_policy(product_client, product_owner, published_product_data, product_service):
    login(product_client)
    values = form_data(published_product_data) | {"course_id": "", "access_mode": "", "access_days": ""}
    assert post(product_client, "/admin/products/new", values).status_code == 303
    assert product_service.get_product(1).data.policy is None
    values.update(access_mode="no_fixed_expiry", access_days="1")
    assert post(product_client, "/admin/products/new", values).status_code == 400
    values.update(access_days="")
    assert post(product_client, "/admin/products/new", values).status_code == 303
    assert product_service.get_product(2).data.policy.access_mode == "no_fixed_expiry"


def test_product_page_logout_uses_owner_revision(product_client, product_owner, active_product):
    login(product_client)
    page = product_client.get("/admin/products/1")
    logout = re.search(r'<form action="/admin/logout".*?</form>', page.text, re.S).group(0)
    owner_revision = re.search(r'name="revision" value="([^"]+)"', logout).group(1)
    assert post(product_client, "/admin/logout", {"revision": owner_revision}).status_code == 303
    assert not product_client.cookies.get("coursmith_admin")


@pytest.mark.parametrize("operation", ["create", "update", "activate", "set_status", "save_category", "record_denial", "inspect"])
def test_sync_product_work_does_not_block_event_loop(
        product_app, product_client, product_owner, active_product, product_service, monkeypatch, operation):
    from course_platform.operations import products

    login(product_client)
    gate, release = Event(), Event()
    target = products if operation == "inspect" else product_service
    name = "inspect_package" if operation == "inspect" else operation
    original = getattr(target, name)

    def delayed(*args, **kwargs):
        gate.set()
        assert release.wait(5), "synchronous product work blocked the event loop"
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, delayed)
    csrf = product_client.cookies.get("coursmith_admin_csrf")
    headers = {"Cookie": f"coursmith_admin={product_client.cookies.get('coursmith_admin')}; coursmith_admin_csrf={csrf}",
               "Origin": "http://testserver"}
    route, data = {
        "create": ("/admin/products/new", form_data(active_product.data) | {"course_id": ""}),
        "update": ("/admin/products/1", form_data(active_product.data) | {"revision": "2"}),
        "activate": ("/admin/products/1/activate", {"revision": "2", **{key: "on" for key in ("quality", "sources", "ai", "mobile", "downloads")}}),
        "inspect": ("/admin/products/1/activate", {"revision": "2", **{key: "on" for key in ("quality", "sources", "ai", "mobile", "downloads")}}),
        "set_status": ("/admin/products/1/pause", {"revision": "2"}),
        "save_category": ("/admin/categories", {"slug": "new", "name": "新", "sort_order": "0"}),
        "record_denial": ("/admin/products/1/pause", {"revision": "bad"}),
    }[operation]

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=product_app), base_url="http://testserver") as client:
            pending = asyncio.create_task(client.post(route, data={"csrf_token": csrf, **data}, headers=headers))
            assert await asyncio.to_thread(gate.wait, 3)
            try:
                response = await asyncio.wait_for(client.get("/static/admin/admin.css"), 1)
                assert response.status_code == 200
                assert not release.is_set(), "product work prevented unrelated request progress"
            finally:
                release.set()
            response = await pending
            assert response.status_code == (409 if operation == "record_denial" else 303)

    watchdog = Thread(target=lambda: (release.wait(4), release.set()), daemon=True)
    watchdog.start()
    try:
        asyncio.run(scenario())
    finally:
        release.set()
        watchdog.join()
