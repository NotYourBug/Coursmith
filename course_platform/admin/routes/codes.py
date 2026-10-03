"""Private code batch HTTP pages with domain-specific denial auditing."""

from __future__ import annotations

import re
import secrets
from datetime import timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from ...database import from_db_time
from ...domain import BusinessError
from ...operations.codes import BatchInput
from .auth import AdminRoute, CSRF_COOKIE, _render, _revision, require_admin_post, require_owner


_POST_ACTIONS = {
    "/admin/code-batches/new": ("code.issue", None, "batch"),
    "/admin/code-batches/{batch_id}/revoke-unused": ("code.void", "batch_id", "batch"),
    "/admin/code-batches/{batch_id}/replace-unused": ("code.reissue", "batch_id", "batch"),
    "/admin/codes/{code_id}/revoke": ("code.void", "code_id", "code"),
    "/admin/codes/{code_id}/replace": ("code.reissue", "code_id", "code"),
}


class CodeRoute(AdminRoute):
    def __init__(self, *args, **kwargs):
        endpoint = kwargs.get("endpoint")
        path = kwargs.get("path", args[0] if args else "")
        boundary = _POST_ACTIONS.get(path)
        if boundary and endpoint is not None and "POST" in (kwargs.get("methods") or []):
            async def endpoint_boundary(request: Request):
                try:
                    return await endpoint(request)
                except BusinessError as error:
                    if error.code != "admin_unavailable" and not getattr(error, "denial_recorded", False):
                        action, key, kind = boundary
                        raw = request.path_params.get(key, "new") if key else "new"
                        object_id = raw if re.fullmatch(r"[0-9]{1,18}", raw) else ("new" if key is None else "invalid")
                        await run_in_threadpool(request.app.state.code_service.record_denial,
                            getattr(request.state, "actor", None), action, object_id, error,
                            request_id=request.state.request_id, kind=kind)
                    raise
            kwargs["endpoint"] = endpoint_boundary
        super().__init__(*args, **kwargs)


router = APIRouter(prefix="/admin", route_class=CodeRoute)


def _integer(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,18}", value):
        raise BusinessError("invalid_batch", "Invalid numeric form field.", 400)
    return int(value)


def _input(form):
    try:
        return BatchInput(product_id=_integer(form.get("product_id", "")),
            count=_integer(form.get("count", "1")), activation_days=_integer(form.get("activation_days", "30")),
            purpose=form.get("purpose", "sale"), note=form.get("note", ""))
    except ValidationError:
        raise BusinessError("invalid_batch", "Use 1–200 codes, 1–365 activation days and at most 1000 note characters.", 400) from None


def _page(request, session, template, **context):
    return _render(request, template, authenticated=True, revision=session.revision,
        csrf_token=request.cookies.get(CSRF_COOKIE, ""), **context)


def _display(value):
    if not value:
        return "—"
    # M1 issues current UTC timestamps. Shanghai uses UTC+08:00; a fixed
    # named zone keeps this display usable on Windows without system tzdata.
    return from_db_time(value).astimezone(timezone(timedelta(hours=8), "Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S Asia/Shanghai")


async def _issued(request, receipt):
    # Only the in-memory first receipt can fill this table. GET/history and
    # idempotent replay have no path back to raw credentials.
    batch, _, _ = await run_in_threadpool(request.app.state.code_service.get_batch, receipt.batch_id)
    return _render(request, "issued_codes.html", authenticated=True,
        revision=request.state.owner_revision, csrf_token=request.cookies.get(CSRF_COOKIE, ""),
        receipt=receipt, note=batch["notes"], display_time=_display)


@router.get("/code-batches", name="code_batch_list")
def batch_list(request: Request):
    session = require_owner(request)
    page = _integer(request.query_params.get("page", "1"))
    batches, total = request.app.state.code_service.list_batches(page=page)
    return _page(request, session, "code_batches.html", batches=batches, total=total,
        display_time=_display, previous=f"/admin/code-batches?page={page - 1}" if page > 1 else None,
        next_page=f"/admin/code-batches?page={page + 1}" if page * 20 < total else None)


@router.get("/code-batches/new", name="code_batch_new")
def batch_form(request: Request):
    session = require_owner(request)
    return _page(request, session, "code_batch_form.html",
        products=request.app.state.code_service.list_issue_products(), idempotency_key=secrets.token_urlsafe(24))


@router.get("/code-batches/{batch_id}", name="code_batch_detail")
def batch_detail(request: Request):
    session = require_owner(request)
    batch_id = _integer(request.path_params["batch_id"])
    page = _integer(request.query_params.get("page", "1"))
    batch, codes, total = request.app.state.code_service.get_batch(batch_id, page=page)
    return _page(request, session, "code_batch_detail.html", batch=batch, codes=codes,
        display_time=_display, idempotency_key=secrets.token_urlsafe(24),
        previous=f"/admin/code-batches/{batch_id}?page={page - 1}" if page > 1 else None,
        next_page=f"/admin/code-batches/{batch_id}?page={page + 1}" if page * 20 < total else None)


async def _post(request):
    actor, form = await require_admin_post(request)
    # Keep the owner's logout revision distinct from code/batch revisions.
    session = await run_in_threadpool(require_owner, request)
    request.state.owner_revision = session.revision
    return actor, form


@router.post("/code-batches/new")
async def batch_issue(request: Request):
    actor, form = await _post(request)
    receipt = await run_in_threadpool(request.app.state.code_service.issue_batch,
        actor, _input(form), form.get("idempotency_key", ""))
    return await _issued(request, receipt)


@router.post("/codes/{code_id}/revoke")
async def code_revoke(request: Request):
    actor, form = await _post(request)
    await run_in_threadpool(request.app.state.code_service.revoke, actor,
        _integer(request.path_params["code_id"]), _revision(form), form.get("reason", ""))
    return RedirectResponse("/admin/code-batches", status_code=303)


@router.post("/codes/{code_id}/replace")
async def code_replace(request: Request):
    actor, form = await _post(request)
    receipt = await run_in_threadpool(request.app.state.code_service.replace, actor,
        _integer(request.path_params["code_id"]), _revision(form), form.get("reason", ""), form.get("idempotency_key", ""))
    return await _issued(request, receipt)


@router.post("/code-batches/{batch_id}/revoke-unused")
async def batch_revoke(request: Request):
    actor, form = await _post(request)
    batch_id = _integer(request.path_params["batch_id"])
    await run_in_threadpool(request.app.state.code_service.revoke_unused, actor,
        batch_id, _revision(form), form.get("reason", ""))
    return RedirectResponse(f"/admin/code-batches/{batch_id}", status_code=303)


@router.post("/code-batches/{batch_id}/replace-unused")
async def batch_replace(request: Request):
    actor, form = await _post(request)
    receipt = await run_in_threadpool(request.app.state.code_service.replace_unused, actor,
        _integer(request.path_params["batch_id"]), _revision(form), form.get("reason", ""), form.get("idempotency_key", ""))
    return await _issued(request, receipt)
