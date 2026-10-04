"""One-shot public credential forms; plaintext is never persisted by HTTP."""
from contextlib import closing

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from ..database import open_readonly
from ..domain import BusinessError
from ..operations.orders import display_time
from ..security import check_origin, parse_unique_form, read_limited_body, source_key
from .catalog import products, render

router = APIRouter()
PUBLIC_COOKIE = "coursmith_public_csrf"


def cookie(request, response, name, token, *, seconds, httponly=True):
    response.set_cookie(name, token, max_age=seconds, path="/", httponly=httponly,
        samesite="lax", secure=request.app.state.settings.environment == "production")


def page(request, restore=False, *, error=None, confirm=False, status=200):
    challenge = request.app.state.csrf_service.issue_challenge("public.credentials")
    items = products(request)
    selected = request.query_params.get("course", "") if not error else ""
    selected = selected if any(item["slug"] == selected for item in items) else ""
    response = render(request, "access_restore.html" if restore else "access.html", status=status,
        error="无法完成请求，请核对凭证后重试。" if error else None, confirm=confirm,
        csrf_token=challenge, courses=items, selected_course=selected)
    cookie(request, response, PUBLIC_COOKIE, challenge, seconds=600)
    if error:
        response.headers.update(getattr(error, "headers", {}))
    return response


def result(request, session, *, raw_key=None, deadline=None):
    with closing(open_readonly(request.app.state.settings.database_path)) as connection:
        row = connection.execute("SELECT slug FROM courses WHERE course_id=?", (session.course_id,)).fetchone()
    slug = row[0]
    seconds = max(1, int((session.session_expires_at - request.app.state.entitlement_service.clock()).total_seconds()))
    response = render(request, "access_result.html", slug=slug, raw_key=raw_key,
        deadline=display_time(deadline) if deadline else None)
    cookie(request, response, f"course_session_{slug}", session.session_id, seconds=seconds)
    cookie(request, response, f"course_csrf_{slug}", session.csrf_token, seconds=seconds)
    response.delete_cookie(PUBLIC_COOKIE, path="/", httponly=True, samesite="lax",
        secure=request.app.state.settings.environment == "production")
    return response


async def boundary(request):
    await run_in_threadpool(request.app.state.rate_limiter.check_public,
        source_key(request, request.app.state.settings.trusted_proxy_cidrs))
    check_origin(request, request.app.state.settings.site_origin)
    body = await read_limited_body(request, 65536)
    if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
        raise BusinessError("invalid_form", "Form required.", 415)
    form = parse_unique_form(body)
    await run_in_threadpool(request.app.state.csrf_service.consume_challenge, "public.credentials",
        form.get("csrf_token", ""), request.cookies.get(PUBLIC_COOKIE, ""), audit_denial=False)
    return form


def selected_course(request, slug):
    with closing(open_readonly(request.app.state.settings.database_path)) as connection:
        row = connection.execute("SELECT course_id FROM courses WHERE slug=?", (slug,)).fetchone()
    if not row:
        raise BusinessError("redemption_denied", "Unavailable.", 403)
    return row[0]


@router.get("/access")
def access_page(request: Request):
    return page(request)


@router.get("/access/restore")
def restore_page(request: Request):
    return page(request, True)


@router.post("/access/redeem")
async def redeem(request: Request):
    try:
        form = await boundary(request)
        slug = form.get("course_slug", "")
        expected = None
        if slug:
            expected = await run_in_threadpool(selected_course, request, slug)
        receipt = await run_in_threadpool(request.app.state.entitlement_service.redeem,
            form.get("code", "").strip(), expected_course_id=expected, request_id=request.state.request_id)
        return await run_in_threadpool(result, request, receipt.session,
            raw_key=receipt.raw_recovery_key, deadline=receipt.entitlement_expires_at)
    except BusinessError as error:
        await run_in_threadpool(request.app.state.entitlement_service.record_denial, None, "code.redeem", None,
            error, request_id=request.state.request_id)
        return await run_in_threadpool(page, request, error=error, status=error.status_code)


@router.post("/access/restore")
async def restore(request: Request):
    try:
        form = await boundary(request)
        if form.get("confirm", "") not in ("", "on"):
            raise BusinessError("recovery_denied", "Unavailable.", 403)
        grant = await run_in_threadpool(request.app.state.recovery_service.restore,
            form.get("credential", "").strip(), evict_oldest=form.get("confirm") == "on",
            request_id=request.state.request_id)
        return await run_in_threadpool(result, request, grant)
    except BusinessError as error:
        await run_in_threadpool(request.app.state.recovery_service.record_denial, None, "recovery.recover", None,
            error, request_id=request.state.request_id)
        return await run_in_threadpool(page, request, True, error=error,
            confirm=error.code == "device_confirmation_required", status=error.status_code)
