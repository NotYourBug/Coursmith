"""Product/category HTTP boundary using the shared owner and CSRF contracts."""

from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from ...domain import BusinessError
from ...operations.products import AccessPolicy, ProductInput, SalesChannel, SalesChecklist
from .auth import AdminRoute, CSRF_COOKIE, _render, _revision, require_admin_post, require_owner


# These actions and object kinds are fixed by the registered route, never by
# request payloads or arbitrary URL text. Auth's mapper intentionally stays local.
_POST_ACTIONS = {
    "/admin/products/new": ("product.create", None),
    "/admin/products/{product_id}": ("product.update", "product_id"),
    "/admin/products/{product_id}/activate": ("product.sales_check", "product_id"),
    "/admin/products/{product_id}/pause": ("product.update", "product_id"),
    "/admin/products/{product_id}/archive": ("product.update", "product_id"),
    "/admin/categories": ("category.create", None),
    "/admin/categories/{category_id}": ("category.update", "category_id"),
}


class ProductRoute(AdminRoute):
    # Wrap the endpoint handler *inside* AdminRoute's error renderer so every
    # failed POST, including authentication/body parsing, reaches our mapper.
    def __init__(self, *args, **kwargs):
        endpoint = kwargs.get("endpoint")
        path = kwargs.get("path", args[0] if args else "")
        boundary = _POST_ACTIONS.get(path)
        if boundary and endpoint is not None:
            async def endpoint_boundary(request: Request):
                try:
                    return await endpoint(request)
                except BusinessError as error:
                    if error.code != "admin_unavailable" and not getattr(error, "denial_recorded", False):
                        action, key = boundary
                        raw = request.path_params.get(key, "new") if key else "new"
                        object_id = raw if re.fullmatch(r"[0-9]{1,18}", raw) else ("new" if key is None else "invalid")
                        await run_in_threadpool(request.app.state.product_service.record_denial,
                            getattr(request.state, "actor", None), action, object_id, error,
                            request_id=request.state.request_id)
                    raise
            # Only async POST endpoints are wrapped; synchronous GET endpoints
            # remain FastAPI thread-pool handlers.
            if "POST" in (kwargs.get("methods") or []):
                kwargs["endpoint"] = endpoint_boundary
        super().__init__(*args, **kwargs)


router = APIRouter(prefix="/admin", route_class=ProductRoute)


def _integer(value, *, signed=False):
    pattern = r"-?[0-9]{1,18}" if signed else r"[0-9]{1,18}"
    if not re.fullmatch(pattern, value):
        raise BusinessError("invalid_product", "Invalid numeric form field.", 400)
    return int(value)


def _checkbox(form, name):
    value = form.get(name, "")
    if value not in ("", "on"):
        raise BusinessError("invalid_product", "Invalid checkbox field.", 400)
    return value == "on"


def _product_input(form):
    try:
        policy = None
        if form.get("access_mode"):
            policy = AccessPolicy(access_mode=form["access_mode"],
                access_days=_integer(form["access_days"]) if form.get("access_days") else None,
                online=_checkbox(form, "online"), pdf=_checkbox(form, "pdf"), zip=_checkbox(form, "zip"),
                update_policy=form.get("update_policy", "current_version"))
        channels = []
        for line in form.get("channels", "").splitlines():
            if not line.strip():
                continue
            name, separator, url = line.partition("|")
            if not separator:
                raise BusinessError("invalid_product", "Use one channel per line: name|HTTPS URL.", 400)
            channels.append(SalesChannel(name=name.strip(), url=url.strip()))
        return ProductInput(title=form.get("title", ""), category_id=_integer(form.get("category_id", "")),
            synopsis=form.get("synopsis", ""), audience=form.get("audience", ""),
            prerequisites=form.get("prerequisites", ""),
            outcomes=[line.strip() for line in form.get("outcomes", "").splitlines() if line.strip()],
            course_id=form.get("course_id") or None, ai_disclosure=form.get("ai_disclosure", ""),
            support_text=form.get("support_text", ""), channels=channels, policy=policy)
    except ValidationError:
        raise BusinessError("invalid_product", "Invalid sales fields, HTTPS channel or explicit learning policy.", 400) from None


def _page(request, session, template, **context):
    return _render(request, template, authenticated=True, revision=session.revision,
                   csrf_token=request.cookies.get(CSRF_COOKIE, ""), **context)


@router.get("/products", name="product_list")
def product_list(request: Request):
    session = require_owner(request)
    query = request.query_params
    status = query.get("status") or None
    category_id = _integer(query["category_id"]) if query.get("category_id") else None
    page = _integer(query.get("page", "1"))
    title = query.get("title", "")
    service = request.app.state.product_service
    products, total = service.list_products(status=status, category_id=category_id, title=title, page=page)
    def page_url(number):
        return str(request.url.replace_query_params(status=status or "", category_id=category_id or "",
                                                     title=title, page=number))
    return _page(request, session, "products.html", products=products, total=total,
        categories=service.list_categories(), status_filter=status, category_id=category_id, title_filter=title, page=page,
        previous=page_url(page - 1) if page > 1 else None, next_page=page_url(page + 1) if page * 20 < total else None)


@router.get("/products/new", name="product_new")
def product_new(request: Request):
    session = require_owner(request)
    service = request.app.state.product_service
    return _page(request, session, "product_form.html", product=None, data=None,
                 categories=service.list_categories(), courses=service.list_courses())


@router.post("/products/new")
async def product_create(request: Request):
    actor, form = await require_admin_post(request)
    record = await run_in_threadpool(request.app.state.product_service.create, actor, _product_input(form))
    return RedirectResponse(f"/admin/products/{record.id}", status_code=303)


@router.get("/products/{product_id}", name="product_detail")
def product_detail(request: Request):
    session = require_owner(request)
    product_id = _integer(request.path_params["product_id"])
    service = request.app.state.product_service
    record = service.get_product(product_id)
    return _page(request, session, "product_detail.html", product=record, data=record.data,
        readiness_error=service.sale_readiness_error(product_id),
        categories=service.list_categories(), courses=service.list_courses())


@router.post("/products/{product_id}")
async def product_update(request: Request):
    actor, form = await require_admin_post(request)
    product_id = _integer(request.path_params["product_id"])
    await run_in_threadpool(request.app.state.product_service.update, actor, product_id, _revision(form), _product_input(form))
    return RedirectResponse(f"/admin/products/{product_id}", status_code=303)


@router.post("/products/{product_id}/activate")
async def product_activate(request: Request):
    actor, form = await require_admin_post(request)
    product_id = _integer(request.path_params["product_id"])
    checks = SalesChecklist(**{key: _checkbox(form, key) for key in ("quality", "sources", "ai", "mobile", "downloads")})
    await run_in_threadpool(request.app.state.product_service.activate, actor, product_id, _revision(form), checks)
    return RedirectResponse(f"/admin/products/{product_id}", status_code=303)


async def _status_post(request, status):
    actor, form = await require_admin_post(request)
    product_id = _integer(request.path_params["product_id"])
    await run_in_threadpool(request.app.state.product_service.set_status, actor, product_id, _revision(form), status)
    return RedirectResponse(f"/admin/products/{product_id}", status_code=303)


@router.post("/products/{product_id}/pause")
async def product_pause(request: Request):
    return await _status_post(request, "paused")


@router.post("/products/{product_id}/archive")
async def product_archive(request: Request):
    return await _status_post(request, "archived")


@router.get("/categories", name="category_list")
def category_list(request: Request):
    session = require_owner(request)
    return _page(request, session, "categories.html", categories=request.app.state.product_service.list_categories())


async def _category_post(request, *, update=False):
    actor, form = await require_admin_post(request)
    category_id = _integer(request.path_params["category_id"]) if update else None
    await run_in_threadpool(request.app.state.product_service.save_category,
        actor, category_id, _revision(form) if category_id is not None else None,
        form.get("slug", ""), form.get("name", ""), _integer(form.get("sort_order", "0"), signed=True),
        _checkbox(form, "enabled"))
    return RedirectResponse("/admin/categories", status_code=303)


@router.post("/categories")
async def category_create(request: Request):
    return await _category_post(request)


@router.post("/categories/{category_id}")
async def category_update(request: Request):
    return await _category_post(request, update=True)
