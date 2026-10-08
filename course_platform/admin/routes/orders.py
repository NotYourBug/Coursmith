"""Private shop-order pages, explicit external confirmation and safe denials."""

import re
import secrets
from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from ...domain import BusinessError
from ...operations.orders import OrderInput, delivery_text, display_time, require_delivery_origin
from .auth import AdminRoute, CSRF_COOKIE, _render, _revision, require_admin_post, require_owner, safe_form_values, field_error
from .codes import _integer


_POST_ACTIONS = {
    "/admin/orders/new": "order.create", "/admin/orders/{order_id}/issue": "order.issue",
    "/admin/orders/{order_id}/confirm-delivery": "order.deliver",
    "/admin/orders/{order_id}/attach-code": "order.attach", "/admin/orders/{order_id}/refund": "order.refund",
    "/admin/orders/search": "order.query",
}


class OrderRoute(AdminRoute):
    def __init__(self, *args, **kwargs):
        endpoint = kwargs.get("endpoint")
        action = _POST_ACTIONS.get(kwargs.get("path", args[0] if args else ""))
        if action and endpoint is not None and "POST" in (kwargs.get("methods") or []):
            async def boundary(request: Request):
                try:
                    return await endpoint(request)
                except BusinessError as error:
                    if error.code != "admin_unavailable" and not getattr(error, "denial_recorded", False):
                        raw = request.path_params.get("order_id", "new")
                        identity = raw if re.fullmatch(r"[0-9]{1,18}|new", raw) else "invalid"
                        await run_in_threadpool(request.app.state.order_service.record_denial,
                            getattr(request.state, "actor", None), action, identity, error,
                            request_id=request.state.request_id)
                    raise
            kwargs["endpoint"] = boundary
        super().__init__(*args, **kwargs)


router = APIRouter(prefix="/admin", route_class=OrderRoute)


def _page(request, session, template, **context):
    return _render(request, template, authenticated=True, revision=session.revision,
        csrf_token=request.cookies.get(CSRF_COOKIE, ""), display_time=display_time, **context)


def _identity(request):
    identity = _integer(request.path_params["order_id"])
    if identity < 1:
        raise BusinessError("invalid_order", "Invalid order ID.", 400)
    return identity


def _confirmation(form):
    if form.get("confirm") != "on":
        raise BusinessError("confirmation_required", "Confirm the shop verification before continuing.", 400)


@router.get("/orders", name="order_list")
def orders(request: Request):
    session = require_owner(request)
    page = _integer(request.query_params.get("page", "1"))
    filters = {key: request.query_params.get(key, "") for key in ("channel", "shop_id", "status")}
    rows, total = request.app.state.order_service.list_orders(page=page, **filters)
    def link(number):
        return "/admin/orders?" + urlencode({**filters, "page": number})
    return _page(request, session, "orders.html", orders=rows, total=total, filters=filters,
        previous=link(page - 1) if page > 1 else None, next_page=link(page + 1) if page * 20 < total else None)


@router.get("/orders/new", name="order_new")
def form(request: Request):
    session = require_owner(request)
    return _page(request, session, "order_form.html", products=request.app.state.code_service.list_issue_products(),
        idempotency_key=secrets.token_urlsafe(24))


@router.post("/orders/search")
async def search(request: Request):
    actor, form = await require_admin_post(request)
    external = form.get("external_order_id", "").strip()
    if not 1 <= len(external) <= 300:
        raise BusinessError("invalid_filter", "Enter the external order number in this private form.", 400)
    page = _integer(form.get("page", "1"))
    filters = {key: form.get(key, "") for key in ("channel", "shop_id", "status")}
    rows, total = await run_in_threadpool(request.app.state.order_service.list_orders,
        page=page, external_order_id=external, **filters)
    session = await run_in_threadpool(require_owner, request)
    return _page(request, session, "orders.html", orders=rows, total=total, filters=filters,
        external_search=external, previous=page - 1 if page > 1 else None, next_page=page + 1 if page * 20 < total else None)


@router.post("/orders/new")
async def record(request: Request):
    actor, form = await require_admin_post(request)
    values = safe_form_values(request, form, ("channel", "shop_id", "external_order_id", "product_id",
        "paid_cents", "paid_at", "note", "confirm", "idempotency_key"))
    def recover(error):
        session = require_owner(request)
        return _page(request, session, "order_form.html", status=error.status_code, error=error,
            products=request.app.state.code_service.list_issue_products(), form_values=values,
            idempotency_key=values["idempotency_key"], field_errors=getattr(error, "field_errors",
                {"paid_at" if error.code == "invalid_payment_time" else "order": error.message}))
    request.state.form_recovery = recover
    if form.get("confirm") != "on":
        raise field_error("confirm", "请核验店铺订单并确认登记。", code="confirmation_required")
    parsed = {}
    for key in ("product_id", "paid_cents"):
        try:
            parsed[key] = _integer(form.get(key, ""))
        except BusinessError:
            raise field_error(key, "请输入非负整数。", code="invalid_order") from None
    try:
        paid_at = datetime.fromisoformat(form.get("paid_at", ""))
    except ValueError:
        raise field_error("paid_at", "请输入含时区的 ISO8601 付款时间。", code="invalid_order") from None
    try:
        # Explicit ISO8601 offset input is parsed at the HTTP boundary only.
        data = OrderInput(channel=form.get("channel", ""), shop_id=form.get("shop_id", ""),
            external_order_id=form.get("external_order_id", ""), product_id=parsed["product_id"],
            paid_cents=parsed["paid_cents"], paid_at=paid_at, note=form.get("note", ""))
    except ValidationError as error:
        failing = str(error.errors(include_input=False)[0]["loc"][0])
        raise field_error(failing, "请核对字段格式、付款时区并删除敏感凭证。", code="invalid_order") from None
    order = await run_in_threadpool(request.app.state.order_service.record, actor, data, form.get("idempotency_key", ""))
    return RedirectResponse(f"/admin/orders/{order.id}", status_code=303)


@router.get("/orders/{order_id}", name="order_detail")
def detail(request: Request):
    session = require_owner(request)
    identity = _identity(request)
    page = _integer(request.query_params.get("page", "1"))
    order, policy, times, codes, total = request.app.state.order_service.get_detail(identity, page=page)
    return _page(request, session, "order_detail.html", order=order, policy=policy, times=times, codes=codes,
        idempotency_key=secrets.token_urlsafe(24), total=total,
        previous=f"/admin/orders/{identity}?page={page - 1}" if page > 1 else None,
        next_page=f"/admin/orders/{identity}?page={page + 1}" if page * 20 < total else None)


@router.post("/orders/{order_id}/issue")
async def issue(request: Request):
    actor, form = await require_admin_post(request)
    identity = _identity(request)
    revision, key = _revision(form), form.get("idempotency_key", "")
    receipt = await run_in_threadpool(request.app.state.order_service.get_issue_replay, actor, identity, revision, key)
    texts = []
    if receipt is None:
        # Validate canonical HTTPS before minting a first code. Replays precede
        # configuration/state checks and contain no plaintext delivery data.
        require_delivery_origin(request.app.state.settings.site_origin)
        policy = await run_in_threadpool(request.app.state.order_service.get_policy, identity)
        receipt = await run_in_threadpool(request.app.state.order_service.issue, actor, identity, revision, key)
        texts = [delivery_text(policy, code, request.app.state.settings.site_origin) for code in receipt.codes]
    session = await run_in_threadpool(require_owner, request)
    return _page(request, session, "issued_codes.html", receipt=receipt, note="", order_id=identity, delivery_texts=texts)


@router.post("/orders/{order_id}/confirm-delivery")
async def confirm_delivery(request: Request):
    actor, form = await require_admin_post(request)
    _confirmation(form)
    identity = _identity(request)
    await run_in_threadpool(request.app.state.order_service.confirm_delivery,
        actor, identity, _revision(form), form.get("idempotency_key", ""))
    return RedirectResponse(f"/admin/orders/{identity}", status_code=303)


@router.post("/orders/{order_id}/attach-code")
async def attach_code(request: Request):
    actor, form = await require_admin_post(request)
    _confirmation(form)
    identity = _identity(request)
    await run_in_threadpool(request.app.state.order_service.attach_code, actor, identity, _revision(form),
        form.get("public_code_id", ""), form.get("verification_reason", ""), form.get("idempotency_key", ""))
    return RedirectResponse(f"/admin/orders/{identity}", status_code=303)


@router.post("/orders/{order_id}/refund")
async def refund(request: Request):
    actor, form = await require_admin_post(request)
    _confirmation(form)
    identity = _identity(request)
    await run_in_threadpool(request.app.state.order_service.record_refund, actor, identity, _revision(form),
        form.get("reason", ""), form.get("idempotency_key", ""))
    return RedirectResponse(f"/admin/orders/{identity}", status_code=303)
