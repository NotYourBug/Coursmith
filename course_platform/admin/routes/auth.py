"""Owner routes and reusable authenticated POST boundary for later tasks."""

from __future__ import annotations

import re
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from ...domain import Actor, BusinessError
from ...security import check_origin, parse_unique_form, read_limited_body, source_key
from ..auth import AdminSession


ADMIN_COOKIE = "coursmith_admin"
CSRF_COOKIE = "coursmith_admin_csrf"
LOGIN_CSRF_COOKIE = "coursmith_admin_login_csrf"
_AUTH_POST_ACTIONS = {
    "/admin/login": "auth.login", "/admin/logout": "auth.logout",
    "/admin/account/password": "admin.password_change",
}


async def _audit_post_denial(request, error):
    action = _AUTH_POST_ACTIONS.get(request.url.path) if request.method == "POST" else None
    # A missing/unmigrated database cannot persist an audit. All initialized
    # auth boundaries use fixed actions and the server's correlation ID.
    if action and error.code != "admin_unavailable" and not getattr(error, "denial_recorded", False):
        await run_in_threadpool(
            request.app.state.admin_service._denial, getattr(request.state, "actor", None),
            action, error, request_id=request.state.request_id,
        )


def _cookie(request, response, name, token, max_age):
    response.set_cookie(name, token, max_age=max_age, path="/admin", httponly=True,
                        samesite="strict", secure=request.app.state.settings.environment == "production")


def _clear_session(request, response):
    for name in (ADMIN_COOKIE, CSRF_COOKIE, LOGIN_CSRF_COOKIE):
        response.delete_cookie(name, path="/admin", httponly=True, samesite="strict",
                               secure=request.app.state.settings.environment == "production")


def _render(request, template, *, status=200, **context):
    return request.app.state.templates.TemplateResponse(
        request=request, name="admin/" + template, context={
            "request_id": request.state.request_id, "error": None,
            "authenticated": False, **context,
        }, status_code=status,
    )


def safe_form_values(request, form, fields):
    """Allowlist recovery only after the caller's complete POST security gate."""
    secrets_to_omit = [value for name, value in form.items()
        if name in {"password", "current_password", "new_password", "confirm_password", "csrf_token", "code", "credential"} and value]
    secrets_to_omit.extend(value for value in request.cookies.values() if value)
    result = {}
    for name in fields:
        value = form.get(name, "")
        if (len(value) <= 16000 and not re.search(r"(?:CS|LK)-[A-Za-z0-9_-]+|(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43,}(?![A-Za-z0-9_-])", value)
            and not any(ord(c) < 32 and c not in "\n\r\t" for c in value)
            and not any(secret in value for secret in secrets_to_omit)):
            result[name] = value
        else:
            result[name] = ""
    return result


def field_error(field, message, *, code="invalid_product"):
    error = BusinessError(code, message, 400)
    error.field_errors = {field: message}
    return error


def _login_page(request, *, error=None, username=""):
    challenge = request.app.state.csrf_service.issue_challenge("admin.login")
    response = _render(request, "login.html", status=error.status_code if error else 200,
                       error=error, username=username, csrf_token=challenge)
    _cookie(request, response, LOGIN_CSRF_COOKIE, challenge, 600)
    if error:
        response.headers.update(getattr(error, "headers", {}))
    return response


class AdminRoute(APIRoute):
    """Keep redirects, errors and successful admin responses private by default."""

    @staticmethod
    def _private_response(request, response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'; img-src 'self'; "
            f"script-src {request.app.state.settings.site_origin}/static/admin/; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def handle(self, scope, receive, send):
        # Starlette rejects unsupported methods before get_route_handler runs.
        if self.methods and scope["method"] not in self.methods:
            request = Request(scope, receive=receive)
            request.state.request_id = secrets.token_hex(16)
            response = _render(request, "error.html", status=405,
                               error=BusinessError("method_not_allowed", "This method is not allowed.", 405))
            response.headers["Allow"] = ", ".join(sorted(self.methods))
            await self._private_response(request, response)(scope, receive, send)
        else:
            await super().handle(scope, receive, send)

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def protected(request):
            request.state.request_id = secrets.token_hex(16)
            try:
                response = await handler(request)
            except BusinessError as error:
                await _audit_post_denial(request, error)
                if request.method == "GET" and error.status_code == 401:
                    response = RedirectResponse("/admin/login", status_code=303)
                else:
                    recovery = getattr(request.state, "form_recovery", None)
                    if error.status_code == 400 and recovery:
                        try:
                            response = await run_in_threadpool(recovery, error)
                        except BusinessError as recovery_error:
                            # Session/lookup state may change between rejection
                            # and recovery. Fail closed without echoing fields.
                            error = recovery_error
                            response = _render(request, "error.html", status=error.status_code, error=error)
                    else:
                        # Conflicts require an explicit GET. No refreshed revision
                        # or retry form is added to the stale POST response.
                        reload_url = request.url.path if error.status_code == 409 else None
                        if reload_url and reload_url.endswith(("/activate", "/pause", "/archive")):
                            reload_url = reload_url.rsplit("/", 1)[0]
                        if reload_url and reload_url.startswith("/admin/categories/"):
                            reload_url = "/admin/categories"
                        response = _render(request, "error.html", status=error.status_code, error=error,
                                           reload_url=reload_url)
                response.headers.update(getattr(error, "headers", {}))
            return self._private_response(request, response)

        return protected


router = APIRouter(prefix="/admin", route_class=AdminRoute)


def require_owner(request: Request) -> AdminSession:
    session = request.app.state.admin_service.require_session(
        request.cookies.get(ADMIN_COOKIE, ""), request_id=request.state.request_id,
    )
    request.state.actor = Actor(session.admin_id, request.state.request_id)
    return session


async def require_admin_post(request: Request) -> tuple[Actor, dict[str, str]]:
    if request.method != "POST":
        raise BusinessError("post_required", "This operation requires POST.", 405)
    session = await run_in_threadpool(require_owner, request)
    check_origin(request, request.app.state.settings.site_origin)
    form = parse_unique_form(await read_limited_body(request, 65536))
    await run_in_threadpool(request.app.state.csrf_service.verify_bound_csrf,
        form.get("csrf_token", ""), request.cookies.get(CSRF_COOKIE, ""), session.csrf_hash,
    )
    return Actor(session.admin_id, request.state.request_id), form


def _revision(form):
    value = form.get("revision", "")
    if not re.fullmatch(r"[0-9]{1,18}", value):
        raise BusinessError("stale_revision", "This form is stale; reload and try again.", 409)
    return int(value)


@router.get("")
@router.get("/")
def home(request: Request):
    session = require_owner(request)
    return _render(request, "base.html", authenticated=True, revision=session.revision,
                   csrf_token=request.cookies.get(CSRF_COOKIE, ""))


@router.get("/login")
def login_page(request: Request):
    request.app.state.admin_service.require_initialized()
    return _login_page(request)


@router.post("/login")
async def login(request: Request):
    await run_in_threadpool(request.app.state.admin_service.require_initialized)
    form = {}
    username = ""
    try:
        check_origin(request, request.app.state.settings.site_origin)
        form = parse_unique_form(await read_limited_body(request, 65536))
        await run_in_threadpool(request.app.state.csrf_service.consume_challenge,
            "admin.login", form.get("csrf_token", ""), request.cookies.get(LOGIN_CSRF_COOKIE, ""),
            audit_denial=False,
        )
        username = safe_form_values(request, form, ("username",)).get("username", "")[:128]
        grant = await run_in_threadpool(request.app.state.admin_service.login,
            form.get("username", ""), form.get("password", ""),
            source=source_key(request, request.app.state.settings.trusted_proxy_cidrs),
            request_id=request.state.request_id, previous_token=request.cookies.get(ADMIN_COOKIE),
        )
    except BusinessError as error:
        await _audit_post_denial(request, error)
        return await run_in_threadpool(_login_page, request, error=error, username=username)
    response = RedirectResponse("/admin", status_code=303)
    _cookie(request, response, ADMIN_COOKIE, grant.token, 28800)
    _cookie(request, response, CSRF_COOKIE, grant.csrf_token, 28800)
    response.delete_cookie(LOGIN_CSRF_COOKIE, path="/admin", httponly=True, samesite="strict",
                           secure=request.app.state.settings.environment == "production")
    return response


@router.post("/logout")
async def logout(request: Request):
    actor, form = await require_admin_post(request)
    if _revision(form) != (await run_in_threadpool(require_owner, request)).revision:
        raise BusinessError("stale_revision", "This form is stale; reload and try again.", 409)
    await run_in_threadpool(request.app.state.admin_service.logout, request.cookies.get(ADMIN_COOKIE, ""), actor)
    response = RedirectResponse("/admin/login", status_code=303)
    _clear_session(request, response)
    return response


@router.get("/account/password")
def password_page(request: Request):
    session = require_owner(request)
    return _render(request, "password.html", authenticated=True, revision=session.revision,
                   csrf_token=request.cookies.get(CSRF_COOKIE, ""))


@router.post("/account/password")
async def password_change(request: Request):
    actor, form = await require_admin_post(request)
    try:
        revision = _revision(form)
        if form.get("new_password", "") != form.get("confirm_password", ""):
            raise BusinessError("password_mismatch", "Password confirmation does not match.", 400)
        await run_in_threadpool(request.app.state.admin_service.change_password,
            actor, form.get("current_password", ""), form.get("new_password", ""),
            expected_revision=revision,
        )
    except BusinessError as error:
        await _audit_post_denial(request, error)
        if error.status_code == 409:
            raise error
        try:
            session = await run_in_threadpool(require_owner, request)
        except BusinessError:
            # A concurrently revoked session must not add a second audit for
            # the original rejection just to render its retry form.
            raise error
        error_field = {
            "invalid_password": "new_password", "password_mismatch": "confirm_password",
            "invalid_credentials": "current_password",
        }.get(error.code)
        return _render(request, "password.html", status=error.status_code, error=error,
                       authenticated=True, revision=session.revision,
                       csrf_token=request.cookies.get(CSRF_COOKIE, ""),
                       field_errors={error_field: error.message} if error_field else {})
    response = RedirectResponse("/admin/login", status_code=303)
    _clear_session(request, response)
    return response
