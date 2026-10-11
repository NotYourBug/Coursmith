"""Native production security, readonly ownership gates and safe input copies."""
from __future__ import annotations

import json
import re
import secrets
from contextlib import closing

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from ...database import open_readonly
from ...domain import BusinessError
from ...production.drafts import DraftService
from ...production.events import record_production_denial
from ...production.projects import ProjectService
from ...security import check_origin
from .auth import AdminRoute, CSRF_COOKIE, _render, require_admin_post, require_owner

BODY_LIMIT = 2097152
SOURCE_LIMIT = 204800
EDITOR_FIELDS = ("title", "audience", "prerequisites", "goal", "difficulty", "chapter_count", "category_id",
                 "mode", "markdown", "text", "source_note", "source_status", "outline_json", "matches_json")
SECRET_FIELDS = frozenset({"password", "current_password", "new_password", "confirm_password", "csrf_token",
    "code", "credential", "secret", "client_secret", "api_key", "apikey", "deepseek_api_key", "authorization", "token", "session", "cookie"})
ACTIONS = {
    "production_create": "production.create", "production_brief": "production.update",
    "production_mode": "production.mode", "production_archive": "production.archive",
    "production_reference_save": "production.reference", "production_reference_remove": "production.reference",
    "production_outline_save": "production.outline", "production_outline_confirm": "production.confirm",
    "production_outline_adopt": "production.candidate", "production_chapter_save": "production.chapter",
    "production_chapter_regenerate": "job.submit", "production_candidate_adopt": "production.candidate",
    "production_candidate_reject": "production.candidate", "production_acknowledge": "production.acknowledge",
    "production_generate": "job.submit", "job_cancel": "job.cancel", "job_resume": "job.resume",
    "job_reconcile": "job.reconcile", "job_check_interrupted": "job.interrupt", "job_test_connection": "job.submit",
}


def integer(value: str, *, field: str = "revision", optional: bool = False, default: int | None = None) -> int | None:
    if type(value) is int:
        value = str(value)  # ASGI integer path converters; form values remain strings.
    if value == "" and optional:
        return default
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,19}", value) or not 1 <= int(value) <= 9223372036854775807:
        error = BusinessError("invalid_" + field, "编号或修订无效，请重新读取页面。", 400)
        error.field_errors = {field: "请输入有效的正整数。"}
        raise error
    return int(value)


def json_value(value: str, *, field: str):
    def pairs(items):
        if len(dict(items)) != len(items):
            raise ValueError
        return dict(items)
    try:
        return json.loads(value, object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError):
        error = BusinessError("invalid_production", "JSON 字段无效，请检查后保存。", 400)
        error.field_errors = {field: "使用有效的 JSON，不能重复字段。"}
        raise error from None


def recover_editor_values(request: Request, form: dict[str, str]) -> dict[str, str]:
    """Called only after complete current session/Origin/CSRF/object gates.

    Long code/base64 is ordinary source. Only named/known credentials and
    recognizable API or delivery-code fragments are excluded; never truncate.
    """
    known = [value for name, value in form.items() if name.lower().replace('-', '_') in SECRET_FIELDS and value]
    known.extend(value for value in request.cookies.values() if value)
    for settings in (request.app.state.settings, request.app.state.production_settings):
        key = getattr(settings, "api_key", "")
        if key:
            known.append(key)
    if any(len(value) > 4096 for value in known):
        return {name: "" for name in EDITOR_FIELDS if name in form}
    fragments = {value[i:i+16] for value in known for i in range(max(0, len(value)-15))}
    result = {}
    for name in EDITOR_FIELDS:
        if name not in form:
            continue
        value = form[name]
        # A complete 60-item outline can exceed a single chapter's source bound;
        # its decoded copy remains bounded by the already admitted raw form.
        copy_limit = BODY_LIMIT if name == "outline_json" else SOURCE_LIMIT
        if (len(value.encode("utf-8")) <= copy_limit
            and not re.search(r"(?:CS|LK)-[A-Za-z0-9_-]+|\bsk-[A-Za-z0-9_-]{8,}", value)
            and not any(secret in value for secret in known)
            and not any(fragment in value for fragment in fragments)
            and not any(ord(c) < 32 and c not in "\n\r\t" for c in value)):
            result[name] = value
        else:
            result[name] = ""
    return result


def authorize_objects(request: Request, actor):
    """Guard every selected child against its authorized project before echo."""
    params = request.path_params
    with closing(open_readonly(request.app.state.settings.database_path)) as conn:
        conn.execute("BEGIN")
        request.app.state.job_service._owner(conn, actor)
        project = None
        if "project_id" in params:
            project = ProjectService._row(conn, integer(params["project_id"], field="identifier"))
            if project["actor_admin_id"] != actor.admin_id:
                raise BusinessError("project_forbidden", "无法访问此制作项目。", 403)
            if "chapter_id" in params:
                DraftService._chapter(conn, project["id"], params["chapter_id"])
            if "candidate_id" in params:
                cid = integer(params["candidate_id"], field="identifier")
                if not conn.execute("SELECT 1 FROM production_candidates WHERE id=? AND project_id=?", (cid, project["id"])).fetchone():
                    raise BusinessError("candidate_missing", "此候选不属于当前项目。", 404)
            reference = params.get("reference_id")
            if reference is not None:
                ProjectService._reference(conn, project["id"], integer(reference, field="identifier"))
        if "job_id" in params:
            job = conn.execute("SELECT * FROM production_jobs WHERE id=?", (integer(params["job_id"], field="identifier"),)).fetchone()
            if job is None:
                raise BusinessError("job_missing", "任务不存在。", 404)
            if job["actor_admin_id"] != actor.admin_id:
                raise BusinessError("job_forbidden", "无法访问此任务。", 403)
            if job["project_id"] is not None:
                project = ProjectService._row(conn, job["project_id"])
                if project["actor_admin_id"] != actor.admin_id:
                    raise BusinessError("project_forbidden", "无法访问此制作项目。", 403)
            if "call_id" in params and not conn.execute("SELECT 1 FROM model_calls WHERE id=? AND job_id=?",
                    (integer(params["call_id"], field="identifier"), job["id"])).fetchone():
                raise BusinessError("model_call_missing", "此调用不属于当前任务。", 404)
        return project["id"] if project else None


def authorize_form_objects(request: Request, form: dict[str, str]):
    """Recheck form-selected existing IDs before any rejected input copy."""
    if "project_id" not in request.path_params:
        return
    project_id = integer(request.path_params["project_id"], field="identifier")
    with closing(open_readonly(request.app.state.settings.database_path)) as conn:
        if form.get("reference_id"):
            ProjectService._reference(conn, project_id, integer(form["reference_id"], field="identifier"))
        selected = [value.strip() for value in form.get("chapter_ids", "").replace(",", "\n").splitlines() if value.strip()]
        for field in ("outline_json", "matches_json"):
            try:
                value = json.loads(form.get(field, ""))
            except (ValueError, TypeError):
                continue  # Syntax errors can keep their authorized text copy.
            if field == "outline_json" and isinstance(value, list):
                selected.extend(item["chapter_id"] for item in value if isinstance(item, dict) and item.get("chapter_id") is not None)
            if field == "matches_json" and isinstance(value, dict):
                selected.extend(cid for cid in value.values() if cid is not None)
        for chapter_id in selected:
            DraftService._chapter(conn, project_id, chapter_id)
        if request.url.path.endswith("/outline/adopt"):
            cid = integer(form.get("candidate_id", ""), field="identifier")
            if not conn.execute("SELECT 1 FROM production_candidates WHERE id=? AND project_id=? AND kind='outline'", (cid, project_id)).fetchone():
                raise BusinessError("candidate_missing", "此大纲候选不属于当前项目。", 404)


def owner_objects(request: Request):
    session = require_owner(request)
    authorize_objects(request, request.state.actor)
    return session


def render(request: Request, template: str, *, status: int = 200, **context):
    session = owner_objects(request)
    return _render(request, template, status=status, authenticated=True, revision=session.revision,
        csrf_token=request.cookies.get(CSRF_COOKIE, ""), idempotency_key=secrets.token_hex(16), **context)


def recover_page(request: Request, error: BusinessError):
    session = owner_objects(request)  # recheck current session/object after service rejection
    check_origin(request, request.app.state.settings.site_origin)
    request.app.state.csrf_service.verify_bound_csrf(request.state.production_form.get("csrf_token", ""),
        request.cookies.get(CSRF_COOKIE, ""), session.csrf_hash)
    authorize_form_objects(request, request.state.production_form)
    return _render(request, "production/editor.html", status=error.status_code, error=error, authenticated=False,
        recovered=recover_editor_values(request, request.state.production_form),
        old_revisions={key: value for key, value in request.state.production_form.items()
            if key in ("revision", "body_revision") and re.fullmatch(r"[0-9]{0,19}", value)},
        reload_url=request.state.production_reload, field_errors=getattr(error, "field_errors", {}))


async def protected_post(request: Request, *, reload_url: str):
    actor, form = await require_admin_post(request, body_limit=BODY_LIMIT)
    await run_in_threadpool(authorize_objects, request, actor)
    await run_in_threadpool(authorize_form_objects, request, form)
    request.state.production_form = form
    request.state.production_reload = reload_url
    return actor, form


class ProductionRoute(AdminRoute):
    """The M1 strategy stays unchanged; production permits bounded400/409 copies."""
    def get_route_handler(self):
        handler = APIRoute.get_route_handler(self)
        async def protected(request):
            request.state.request_id = secrets.token_hex(16)
            try:
                response = await handler(request)
            except (BusinessError, ValidationError) as exc:
                if isinstance(exc, ValidationError):
                    error = BusinessError("invalid_production", "制作字段无效，请检查字段后保存。", 400)
                    error.field_errors = {str(item["loc"][0]): "字段超限或格式无效。" for item in exc.errors(include_input=False) if item["loc"]}
                else:
                    error = exc
                action = ACTIONS.get(self.name) if request.method == "POST" else None
                if action and error.code != "admin_unavailable":
                    await run_in_threadpool(record_production_denial, request.app.state.settings.database_path,
                        getattr(request.state, "actor", None), action,
                        request.path_params.get("project_id", request.path_params.get("job_id", "new")), error,
                        request_id=request.state.request_id)
                if request.method == "GET" and error.status_code == 401:
                    response = RedirectResponse("/admin/login", status_code=303)
                elif error.status_code in (400, 409) and hasattr(request.state, "production_form"):
                    try:
                        response = await run_in_threadpool(recover_page, request, error)
                    except BusinessError as revoked:
                        response = _render(request, "error.html", status=revoked.status_code, error=revoked)
                else:
                    response = _render(request, "error.html", status=error.status_code, error=error)
                response.headers.update(getattr(error, "headers", {}))
            return self._private_response(request, response)
        return protected
