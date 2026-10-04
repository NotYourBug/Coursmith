"""Private explicit legacy verification; credential reset remains a separate POST."""

import re
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from ...domain import BusinessError
from ...operations.orders import display_time
from ...operations.products import AccessPolicy
from .auth import AdminRoute, CSRF_COOKIE, _render, _revision, require_admin_post, require_owner
from .products import _checkbox, _integer


class LegacyRoute(AdminRoute):
    def __init__(self, *args, **kwargs):
        endpoint = kwargs.get("endpoint")
        if endpoint is not None and "POST" in (kwargs.get("methods") or []):
            async def boundary(request: Request):
                try:
                    return await endpoint(request)
                except BusinessError as error:
                    if error.code != "admin_unavailable" and not getattr(error, "denial_recorded", False):
                        kind = "entitlement" if "entitlement_id" in request.path_params else "code"
                        raw = request.path_params.get(kind + "_id", "")
                        identity = int(raw) if re.fullmatch(r"[0-9]{1,18}", raw) else None
                        await run_in_threadpool(request.app.state.legacy_service.record_denial,
                            getattr(request.state, "actor", None), kind, identity, error,
                            request_id=request.state.request_id)
                    raise
            kwargs["endpoint"] = boundary
        super().__init__(*args, **kwargs)


router = APIRouter(prefix="/admin", route_class=LegacyRoute)


def _policy(form):
    if not form.get("access_mode"):
        return None
    try:
        return AccessPolicy(access_mode=form["access_mode"],
            access_days=_integer(form["access_days"]) if form.get("access_days") else None,
            online=_checkbox(form, "online"), pdf=_checkbox(form, "pdf"), zip=_checkbox(form, "zip"),
            update_policy=form.get("update_policy", ""))
    except ValidationError:
        raise BusinessError("invalid_policy", "Choose every policy field explicitly.", 400) from None


async def _post(request, kind):
    actor, form = await require_admin_post(request)
    if form.get("confirm") != "on":
        raise BusinessError("confirmation_required", "Confirm the original holder, evidence and original/new deadline choices.", 400)
    identity = _integer(request.path_params[kind + "_id"])
    if identity < 1:
        raise BusinessError("invalid_legacy", "Choose an existing preserved record.", 400)
    return actor, form, identity, _revision(form)


@router.get("/legacy-codes", name="legacy_codes")
def codes(request: Request):
    session = require_owner(request)
    page = _integer(request.query_params.get("page", "1"))
    codes, total = request.app.state.legacy_service.list_codes(page=page)
    entitlements, rights_total = request.app.state.legacy_service.list_entitlements(page=page)
    return _render(request, "legacy_codes.html", authenticated=True, revision=session.revision,
        csrf_token=request.cookies.get(CSRF_COOKIE, ""), codes=codes, entitlements=entitlements,
        display_time=display_time, previous=f"/admin/legacy-codes?page={page-1}" if page > 1 else None,
        next_page=f"/admin/legacy-codes?page={page+1}" if page*20 < max(total, rights_total) else None)


@router.post("/legacy-codes/{code_id}/resolve")
async def resolve(request: Request):
    actor, form, identity, revision = await _post(request, "code")
    await run_in_threadpool(request.app.state.legacy_service.resolve_code, actor, identity, revision,
        _policy(form), form.get("purpose", ""), form.get("verification_reason", ""),
        activation_days=_integer(form["activation_days"]) if form.get("activation_days") else None)
    return RedirectResponse("/admin/legacy-codes", status_code=303)


@router.post("/entitlements/{entitlement_id}/verify-legacy")
async def verify(request: Request):
    actor, form, identity, revision = await _post(request, "entitlement")
    try:
        expiry = datetime.fromisoformat(form["expires_at"]) if form.get("expires_at") else None
    except ValueError:
        raise BusinessError("explicit_deadline", "Enter an explicit ISO8601 learning deadline with timezone.", 400) from None
    await run_in_threadpool(request.app.state.legacy_service.verify_entitlement, actor, identity, revision,
        _integer(form["order_id"]) if form.get("order_id") else None, _policy(form), expiry,
        form.get("purpose", ""), form.get("verification_reason", ""), form.get("idempotency_key", ""))
    return RedirectResponse(f"/admin/entitlements/{identity}", status_code=303)
