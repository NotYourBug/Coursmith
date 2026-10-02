"""Owner routes and reusable authenticated POST boundary for later tasks."""

from __future__ import annotations

import re
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.routing import APIRoute

from ...domain import Actor, BusinessError
from ...security import check_origin, parse_unique_form, read_limited_body, source_key
from ..auth import AdminSession


ADMIN_COOKIE = "coursmith_admin"
CSRF_COOKIE = "coursmith_admin_csrf"
LOGIN_CSRF_COOKIE = "coursmith_admin_login_csrf"


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

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def protected(request):
            request.state.request_id = secrets.token_hex(16)
            try:
                response = await handler(request)
            except BusinessError as error:
                if request.method == "GET" and error.status_code == 401:
                    response = RedirectResponse("/admin/login", status_code=303)
                else:
                    response = _render(request, "error.html", status=error.status_code, error=error)
                response.headers.update(getattr(error, "headers", {}))
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Request-ID"] = request.state.request_id
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; style-src 'self'; img-src 'self'; "
                f"script-src {request.app.state.settings.site_origin}/static/admin/; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
            )
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response

        return protected


router = APIRouter(prefix="/admin", route_class=AdminRoute)


def require_owner(request: Request) -> AdminSession:
    return request.app.state.admin_service.require_session(
        request.cookies.get(ADMIN_COOKIE, ""), request_id=request.state.request_id,
    )


async def require_admin_post(request: Request) -> tuple[Actor, dict[str, str]]:
    if request.method != "POST":
        raise BusinessError("post_required", "This operation requires POST.", 405)
    session = require_owner(request)
    check_origin(request, request.app.state.settings.site_origin)
    form = parse_unique_form(await read_limited_body(request, 65536))
    request.app.state.csrf_service.verify_bound_csrf(
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
    request.app.state.admin_service.require_initialized()
    form = {}
    try:
        check_origin(request, request.app.state.settings.site_origin)
        form = parse_unique_form(await read_limited_body(request, 65536))
        request.app.state.csrf_service.consume_challenge(
            "admin.login", form.get("csrf_token", ""), request.cookies.get(LOGIN_CSRF_COOKIE, ""),
        )
        grant = request.app.state.admin_service.login(
            form.get("username", ""), form.get("password", ""),
            source=source_key(request, request.app.state.settings.trusted_proxy_cidrs),
            request_id=request.state.request_id, previous_token=request.cookies.get(ADMIN_COOKIE),
        )
    except BusinessError as error:
        return _login_page(request, error=error, username=form.get("username", "")[:128])
    response = RedirectResponse("/admin", status_code=303)
    _cookie(request, response, ADMIN_COOKIE, grant.token, 28800)
    _cookie(request, response, CSRF_COOKIE, grant.csrf_token, 28800)
    response.delete_cookie(LOGIN_CSRF_COOKIE, path="/admin", httponly=True, samesite="strict",
                           secure=request.app.state.settings.environment == "production")
    return response


@router.post("/logout")
async def logout(request: Request):
    actor, form = await require_admin_post(request)
    if _revision(form) != require_owner(request).revision:
        raise BusinessError("stale_revision", "This form is stale; reload and try again.", 409)
    request.app.state.admin_service.logout(request.cookies.get(ADMIN_COOKIE, ""), actor)
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
        request.app.state.admin_service.change_password(
            actor, form.get("current_password", ""), form.get("new_password", ""),
            expected_revision=revision,
        )
    except BusinessError as error:
        session = require_owner(request)
        return _render(request, "password.html", status=error.status_code, error=error,
                       authenticated=True, revision=session.revision,
                       csrf_token=request.cookies.get(CSRF_COOKIE, ""))
    response = RedirectResponse("/admin/login", status_code=303)
    _clear_session(request, response)
    return response
