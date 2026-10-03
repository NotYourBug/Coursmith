"""Owner credential support using the shared private POST boundary."""

import re
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from ...domain import BusinessError
from .auth import AdminRoute, CSRF_COOKIE, _render, _revision, require_admin_post, require_owner
from .codes import _display


_POST_ACTIONS = {
    "/admin/entitlements/{entitlement_id}/reset-credential": "recovery.reset",
    "/admin/entitlements/{entitlement_id}/revoke": "entitlement.revoke",
}


class EntitlementRoute(AdminRoute):
    def __init__(self, *args, **kwargs):
        endpoint = kwargs.get("endpoint")
        action = _POST_ACTIONS.get(kwargs.get("path", args[0] if args else ""))
        if action and endpoint is not None and "POST" in (kwargs.get("methods") or []):
            async def boundary(request: Request):
                try:
                    return await endpoint(request)
                except BusinessError as error:
                    if error.code != "admin_unavailable" and not getattr(error, "denial_recorded", False):
                        raw = request.path_params.get("entitlement_id", "")
                        identity = int(raw) if re.fullmatch(r"[0-9]{1,18}", raw) else None
                        await run_in_threadpool(request.app.state.recovery_service.record_denial,
                            getattr(request.state, "actor", None), action, identity, error,
                            request_id=request.state.request_id)
                    raise
            kwargs["endpoint"] = boundary
        super().__init__(*args, **kwargs)


router = APIRouter(prefix="/admin", route_class=EntitlementRoute)


def _identity(request):
    value = request.path_params["entitlement_id"]
    if not re.fullmatch(r"[0-9]{1,18}", value) or int(value) < 1:
        raise BusinessError("invalid_entitlement", "Invalid entitlement ID.", 400)
    return int(value)


@router.get("/entitlements/{entitlement_id}", name="entitlement_detail")
def detail(request: Request):
    session = require_owner(request)
    entitlement = request.app.state.recovery_service.get_detail(_identity(request))
    return _render(request, "entitlement_detail.html", authenticated=True, revision=session.revision,
        csrf_token=request.cookies.get(CSRF_COOKIE, ""), entitlement=entitlement,
        idempotency_key=secrets.token_urlsafe(24), display_time=_display)


async def _post(request):
    actor, form = await require_admin_post(request)
    if form.get("confirm") != "on":
        raise BusinessError("confirmation_required", "Confirm the recorded verification and impact before continuing.", 400)
    return actor, form, _identity(request), _revision(form)


@router.post("/entitlements/{entitlement_id}/reset-credential")
async def reset(request: Request):
    actor, form, identity, revision = await _post(request)
    receipt = await run_in_threadpool(request.app.state.recovery_service.reset,
        actor, identity, revision, form.get("reason", ""), form.get("idempotency_key", ""))
    session = await run_in_threadpool(require_owner, request)
    return _render(request, "credential_result.html", authenticated=True, revision=session.revision,
        csrf_token=request.cookies.get(CSRF_COOKIE, ""), receipt=receipt)


@router.post("/entitlements/{entitlement_id}/revoke")
async def revoke(request: Request):
    actor, form, identity, revision = await _post(request)
    await run_in_threadpool(request.app.state.entitlement_service.revoke,
        actor, identity, revision, form.get("reason", ""), form.get("idempotency_key", ""))
    return RedirectResponse(f"/admin/entitlements/{identity}", status_code=303)
