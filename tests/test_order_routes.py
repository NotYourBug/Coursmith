"""Task9 owner pages exercise actual POST security and domain services."""

import re
import asyncio
from dataclasses import replace
from threading import Event, Thread

import httpx
import pytest
from fastapi.testclient import TestClient

from course_platform.admin.routes.orders import router
from test_product_routes import login, post


@pytest.fixture
def order_app(http_app_factory, admin_settings, admin_service, csrf_service, rate_limiter,
        order_service, code_service, product_service, entitlement_service, recovery_service, progress_service):
    from course_platform.admin.routes.auth import router as auth_router
    from course_platform.admin.routes.codes import router as code_router
    from course_platform.admin.routes.products import router as product_router
    from course_platform.admin.routes.entitlements import router as entitlement_router
    return http_app_factory([auth_router, product_router, code_router, entitlement_router, router], admin_settings, {
        "admin_service": admin_service, "csrf_service": csrf_service, "rate_limiter": rate_limiter,
        "order_service": order_service, "code_service": code_service, "product_service": product_service,
        "entitlement_service": entitlement_service, "recovery_service": recovery_service, "progress_service": progress_service})


@pytest.fixture
def order_client(order_app):
    with TestClient(order_app, client=("198.51.100.7", 50000), follow_redirects=False) as client:
        yield client


def fields(**changes):
    return {"channel": "taobao", "shop_id": "shop-1", "external_order_id": "private-external-order-1",
        "product_id": "1", "paid_cents": "0", "paid_at": "2026-10-02T00:00:00+00:00",
        "note": "核验已付款", "confirm": "on", "revision": "1", "idempotency_key": "http-record", **changes}


def test_owner_order_record_issue_delivery_and_refund(order_client, active_product):
    for path in ("/admin/orders", "/admin/orders/new", "/admin/orders/1"):
        assert order_client.get(path).status_code == 303
    login(order_client)
    form = order_client.get("/admin/orders/new")
    assert form.status_code == 200 and "不支持多商品订单" in form.text
    created = post(order_client, "/admin/orders/new", fields())
    assert created.status_code == 303 and created.headers["location"] == "/admin/orders/1"
    listing = order_client.get("/admin/orders")
    assert "private-external-order-1" not in listing.text
    detail = order_client.get("/admin/orders/1")
    assert "private-external-order-1" in detail.text and "Asia/Shanghai" in detail.text
    # A delivery copy always uses the configured canonical HTTPS origin.
    order_client.app.state.settings = replace(order_client.app.state.settings, site_origin="https://courses.example")
    issued = order_client.post("/admin/orders/1/issue", data={"csrf_token": order_client.cookies.get("coursmith_admin_csrf"),
        "revision": "1", "idempotency_key": "http-issue"}, headers={"Origin": "https://courses.example"})
    assert issued.status_code == 200 and issued.headers["cache-control"] == "no-store"
    raw = re.search(r"CS-[A-Za-z0-9_-]{32}", issued.text).group()
    assert "待人工确认已在店铺发送" in issued.text and "https://courses.example" in issued.text
    assert raw not in order_client.get("/admin/orders/1").text
    order_client.app.state.settings = replace(order_client.app.state.settings, site_origin="http://testserver")
    assert post(order_client, "/admin/orders/1/confirm-delivery", fields(revision="2", idempotency_key="delivery")).status_code == 303
    assert post(order_client, "/admin/orders/1/refund", fields(revision="3", reason="核验外部已退款", idempotency_key="refund")).status_code == 303
    assert "已退款" in order_client.get("/admin/orders/1").text


@pytest.mark.parametrize("path", ["/admin/orders/new", "/admin/orders/search", "/admin/orders/1/issue", "/admin/orders/1/confirm-delivery",
    "/admin/orders/1/attach-code", "/admin/orders/1/refund"])
@pytest.mark.parametrize("fault,status", [("auth", 401), ("csrf", 403), ("origin", 403), ("body", 413)])
def test_order_post_boundaries(order_client, active_product, db_path, path, fault, status):
    from course_platform.database import transaction
    if fault != "auth":
        login(order_client)
    headers = {"Origin": "https://evil.example" if fault == "origin" else "http://testserver"}
    response = order_client.post(path, content=b"x" * 65537, headers={**headers, "Content-Type": "application/x-www-form-urlencoded"}) if fault == "body" else order_client.post(path,
        data={**fields(), "csrf_token": "bad" if fault == "csrf" else order_client.cookies.get("coursmith_admin_csrf", "")}, headers=headers)
    assert response.status_code == status and response.headers["cache-control"] == "no-store"
    with transaction(db_path) as connection:
        events = connection.execute("SELECT object_type, object_id FROM admin_events WHERE action LIKE 'order.%' AND outcome='denied'").fetchall()
        assert len(events) == 1 and events[0]["object_type"] == "order"
        assert connection.execute("SELECT count(*) FROM orders").fetchone()[0] == 0


def test_issue_replay_is_metadata_after_refund_and_origin_change(order_client, active_product,
        order_service, actor, clock, db_path):
    from course_platform.operations.orders import OrderInput
    order = order_service.record(actor, OrderInput(channel="taobao", shop_id="shop-1", external_order_id="private",
        product_id=active_product.id, paid_cents=0, paid_at=clock.now(), note="核验"), "record")
    issued = order_service.issue(actor, order.id, 1, "replay-key")
    order_service.record_refund(actor, order.id, 2, "核验店铺已退款", "refund")
    login(order_client)
    # Current development HTTP origin differs from the former delivery HTTPS origin.
    replay = post(order_client, "/admin/orders/1/issue", {"revision": "1", "idempotency_key": "replay-key"})
    assert replay.status_code == 200 and "重放只返回批次信息" in replay.text
    assert issued.codes[0].raw_code not in replay.text
    assert issued.codes[0].raw_code.encode() not in db_path.read_bytes()


def test_http_real_sale_reset_preserves_progress_and_rejects_old_access(order_client, active_product,
        entitlement_service, recovery_service, progress_service, db_path):
    from course_platform.database import transaction
    from course_platform.domain import BusinessError
    login(order_client)
    assert post(order_client, "/admin/orders/new", fields()).status_code == 303
    order_client.app.state.settings = replace(order_client.app.state.settings, site_origin="https://courses.example")
    issued = order_client.post("/admin/orders/1/issue", data={"csrf_token": order_client.cookies.get("coursmith_admin_csrf"),
        "revision": "1", "idempotency_key": "http-issue"}, headers={"Origin": "https://courses.example"})
    assert issued.status_code == 200
    raw = re.search(r"CS-[A-Za-z0-9_-]{32}", issued.text).group()
    order_client.app.state.settings = replace(order_client.app.state.settings, site_origin="http://testserver")
    buyer = entitlement_service.redeem(raw, expected_course_id=None, request_id="real-http-sale")
    progress_service.set_completed(buyer.session.session_id, buyer.session.course_id, 1, True)
    detail = order_client.get("/admin/orders/1")
    assert 'href="/admin/entitlements/1"' in detail.text and "已激活" in detail.text
    reset = post(order_client, "/admin/entitlements/1/reset-credential", {"revision": "1", "confirm": "on",
        "reason": "已核验店铺付款及凭证遗失", "idempotency_key": "http-reset"})
    assert reset.status_code == 200
    key = re.search(r"LK-[A-Za-z0-9_-]{43}", reset.text).group()
    with pytest.raises(BusinessError):
        entitlement_service.require_session(buyer.session.session_id, buyer.session.course_id)
    with pytest.raises(BusinessError):
        recovery_service.restore(buyer.raw_recovery_key, request_id="old-http-key")
    restored = recovery_service.restore(key, request_id="new-http-key")
    assert progress_service.get_progress(restored.session_id, restored.course_id) == {1: True}
    with transaction(db_path) as connection:
        before = tuple(connection.execute("SELECT expires_at, verified_at, verified_by, verified_reason FROM entitlements").fetchone())
    replay = post(order_client, "/admin/entitlements/1/reset-credential", {"revision": "1", "confirm": "on",
        "reason": "已核验店铺付款及凭证遗失", "idempotency_key": "http-reset"})
    assert replay.status_code == 200 and key not in replay.text
    with transaction(db_path) as connection:
        assert tuple(connection.execute("SELECT expires_at, verified_at, verified_by, verified_reason FROM entitlements").fetchone()) == before
    assert key.encode() not in db_path.read_bytes() and raw.encode() not in db_path.read_bytes()


@pytest.mark.parametrize("amount", ["-1", "True", "1.2"])
def test_http_rejects_invalid_money(order_client, active_product, amount):
    login(order_client)
    assert post(order_client, "/admin/orders/new", fields(paid_cents=amount)).status_code == 400


@pytest.mark.parametrize("operation", ["confirm-delivery", "attach-code", "refund"])
def test_actions_require_explicit_confirmation_and_post(order_client, active_product,
        order_service, actor, clock, operation):
    from course_platform.operations.orders import OrderInput
    order_service.record(actor, OrderInput(channel="shop", shop_id="one", external_order_id="private",
        product_id=active_product.id, paid_cents=0, paid_at=clock.now(), note="核验"), "record")
    login(order_client)
    assert post(order_client, f"/admin/orders/1/{operation}", fields(confirm="")).status_code == 400
    assert order_client.get(f"/admin/orders/1/{operation}").status_code == 405
    assert order_service.get_order(1).revision == 1


def test_attach_code_http_uses_public_number_and_real_verification(order_client, active_product,
        code_service, entitlement_service, db_path):
    from course_platform.operations.codes import BatchInput
    from course_platform.domain import Actor
    from course_platform.database import transaction
    code = code_service.issue_batch(Actor(1, "trusted"), BatchInput(product_id=active_product.id, purpose="sale"), "unbound").codes[0]
    entitlement_service.redeem(code.raw_code, expected_course_id=None, request_id="unbound")
    login(order_client)
    assert post(order_client, "/admin/orders/new", fields()).status_code == 303
    attached = post(order_client, "/admin/orders/1/attach-code", fields(public_code_id=code.public_id,
        verification_reason="已核对原码对应的店铺付款记录", idempotency_key="http-attach"))
    assert attached.status_code == 303
    assert 'href="/admin/entitlements/1"' in order_client.get("/admin/orders/1").text
    with transaction(db_path) as connection:
        assert tuple(connection.execute("SELECT count(*), order_id FROM entitlements").fetchone()) == (1, 1)


def test_order_detail_does_not_echo_unsafe_historical_refund_reason(order_client, active_product,
        order_service, actor, clock, db_path):
    from course_platform.database import transaction
    from course_platform.operations.orders import OrderInput
    import secrets
    order_service.record(actor, OrderInput(channel="shop", shop_id="one", external_order_id="private",
        product_id=active_product.id, paid_cents=0, paid_at=clock.now(), note="核验"), "record")
    order_service.record_refund(actor, 1, 1, "核验退款", "refund")
    raw = "LK-" + secrets.token_urlsafe(32)
    with transaction(db_path) as connection:
        connection.execute("UPDATE orders SET refund_reason=?", (raw,))
    login(order_client)
    response = order_client.get("/admin/orders/1")
    assert response.status_code == 200
    if raw in response.text:
        pytest.fail("Historical credentials must not be echoed")


def test_exact_external_order_search_is_private_post_and_paginates(order_client, active_product,
        order_service, actor, clock):
    from course_platform.operations.orders import OrderInput
    for index in range(21):
        order_service.record(actor, OrderInput(channel="shop", shop_id=f"shop-{index}", external_order_id="private-search-order",
            product_id=active_product.id, paid_cents=0, paid_at=clock.now(), note="核验"), f"record-{index}")
    login(order_client)
    first = post(order_client, "/admin/orders/search", {"external_order_id": "private-search-order", "page": "1"})
    assert first.status_code == 200 and "共 21 项" in first.text
    assert 'method="post" action="/admin/orders/search"' in first.text
    assert "private-search-order" not in " ".join(re.findall(r'href="([^"]*)"', first.text))
    assert len(re.findall(r'href="/admin/orders/[0-9]+"', first.text)) == 20
    second = post(order_client, "/admin/orders/search", {"external_order_id": "private-search-order", "page": "2"})
    assert second.status_code == 200 and len(re.findall(r'href="/admin/orders/[0-9]+"', second.text)) == 1
    response = order_client.get("/admin/orders/search")
    assert response.status_code in (400, 405) and "private-search-order" not in response.text


@pytest.mark.parametrize("fault", ["paid_at", "note"])
def test_unverified_or_unsafe_historical_order_has_safe_error(order_client, active_product,
        order_service, actor, clock, db_path, fault):
    from course_platform.database import transaction
    from course_platform.operations.orders import OrderInput
    order_service.record(actor, OrderInput(channel="shop", shop_id="one", external_order_id="private",
        product_id=active_product.id, paid_cents=0, paid_at=clock.now(), note="核验"), "record")
    with transaction(db_path) as connection:
        connection.execute("UPDATE orders SET paid_at=NULL" if fault == "paid_at" else "UPDATE orders SET notes='LK-invalid-history'")
    login(order_client)
    response = order_client.get("/admin/orders/1")
    assert response.status_code == 409 and response.headers["cache-control"] == "no-store"
    assert "LK-invalid-history" not in response.text


@pytest.mark.parametrize("operation", ["record", "issue", "get_issue_replay", "confirm_delivery", "attach_code", "record_refund", "record_denial", "list_orders"])
def test_order_db_work_is_offloaded(order_app, order_client, active_product, order_service,
        code_service, actor, clock, monkeypatch, operation):
    from course_platform.operations.codes import BatchInput
    from course_platform.operations.orders import OrderInput
    order_service.record(actor, OrderInput(channel="shop", shop_id="one", external_order_id="private",
        product_id=active_product.id, paid_cents=0, paid_at=clock.now(), note="核验"), "record")
    code = code_service.issue_batch(actor, BatchInput(product_id=active_product.id, purpose="sale"), "unbound").codes[0]
    if operation == "confirm_delivery":
        order_service.issue(actor, 1, 1, "initial")
    login(order_client)
    if operation in ("issue", "get_issue_replay"):
        order_app.state.settings = replace(order_app.state.settings, site_origin="https://courses.example")
    gate, release = Event(), Event()
    original = getattr(order_service, operation)
    def delayed(*args, **kwargs):
        gate.set()
        assert release.wait(5), "Order DB work blocked the event loop"
        return original(*args, **kwargs)
    monkeypatch.setattr(order_service, operation, delayed)
    path = {"record": "/admin/orders/new", "issue": "/admin/orders/1/issue", "get_issue_replay": "/admin/orders/1/issue",
        "confirm_delivery": "/admin/orders/1/confirm-delivery", "attach_code": "/admin/orders/1/attach-code",
        "record_refund": "/admin/orders/1/refund", "record_denial": "/admin/orders/1/refund",
        "list_orders": "/admin/orders/search"}[operation]
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=order_app), base_url="http://testserver",
                cookies=dict(order_client.cookies.items())) as client:
            pending = asyncio.create_task(client.post(path, data={**fields(external_order_id="another", public_code_id=code.public_id,
                verification_reason="核验付款", reason="核验店铺退款", idempotency_key="offload",
                revision="bad" if operation == "record_denial" else "2" if operation == "confirm_delivery" else "1"),
                "csrf_token": order_client.cookies.get("coursmith_admin_csrf")}, headers={"Origin": order_app.state.settings.site_origin}))
            assert await asyncio.to_thread(gate.wait, 3)
            try:
                assert (await asyncio.wait_for(client.get("/static/admin/admin.css"), 1)).status_code == 200
                assert not release.is_set()
            finally:
                release.set()
            response = await pending
            assert response.status_code == (409 if operation == "record_denial" else 200 if operation in ("issue", "get_issue_replay", "list_orders") else 303)
    watchdog = Thread(target=lambda: (release.wait(4), release.set()), daemon=True)
    watchdog.start()
    try:
        asyncio.run(scenario())
    finally:
        release.set()
        watchdog.join()
