"""Protected native project, outline, reference and chapter operations."""
from __future__ import annotations

import json
from contextlib import closing
from dataclasses import replace
from datetime import timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from ...database import from_db_time, open_readonly
from ...domain import BusinessError
from ...jobs.models import JobRequest
from ...production.models import BriefInput, OutlineInput, ReferenceInput
from ...production.checks import validate_draft
from ...production.rendering import render_markdown
from .production_forms import ProductionRoute, integer, json_value, owner_objects, protected_post, render

router = APIRouter(prefix="/admin/production", route_class=ProductionRoute)


def _base(project_id):
    return f"/admin/production/{project_id}"


def _brief(form):
    return BriefInput(title=form.get("title", ""), audience=form.get("audience", ""),
        prerequisites=form.get("prerequisites", ""), goal=form.get("goal", ""), difficulty=form.get("difficulty", ""),
        chapter_count=integer(form.get("chapter_count", ""), field="chapter_count", optional=True, default=10),
        category_id=integer(form.get("category_id", ""), field="category_id", optional=True), content_kind="course")


def _selection(value):
    return tuple(item.strip() for item in value.replace(",", "\n").splitlines() if item.strip())


def _preview(markdown):
    # Only the existing sanitized renderer output receives fixed scroll markup.
    result = render_markdown(markdown)
    return replace(result, html=result.html.replace("<table>",
        '<div class="table-scroll" role="region" aria-label="表格，可横向滚动" tabindex="0"><table>')
        .replace("</table>", "</table></div>"))


def _local_time(value):
    if not value:
        return "—"
    # Contemporary M2 timestamps use UTC+08; avoid Windows IANA/tzdata reliance.
    return from_db_time(value).astimezone(timezone(timedelta(hours=8), "Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S Asia/Shanghai")


def _data(request, project_id):
    owner_objects(request)
    state = request.app.state
    project = state.production_service.get(project_id)
    frozen = state.draft_service.freeze_input(project_id)
    with closing(open_readonly(state.settings.database_path)) as conn:
        candidates = [dict(row) for row in conn.execute("SELECT id,kind,chapter_id,state FROM production_candidates WHERE project_id=? ORDER BY id DESC", (project_id,))]
        chapters = [dict(row) for row in conn.execute("SELECT id,needs_review,review_reason FROM production_chapters WHERE project_id=? AND active=1", (project_id,))]
        jobs = [dict(row) for row in conn.execute("SELECT id,kind,state,reason_code FROM production_jobs WHERE project_id=? ORDER BY id DESC LIMIT 20", (project_id,))]
        categories = [dict(row) for row in conn.execute("SELECT id,name FROM categories ORDER BY id")]
    return dict(project=project, frozen=frozen, candidates=candidates, chapter_flags={c["id"]: c for c in chapters},
        project_jobs=jobs, categories=categories, values=project.brief.model_dump(),
        outline_json=json.dumps([item.model_dump() for item in frozen.outline.items] if frozen.outline else [], ensure_ascii=False, indent=2),
        settings=state.production_settings, draft_check=validate_draft(frozen))


async def _write(request, project_id, method, *args, **kwargs):
    actor, form = await protected_post(request, reload_url=_base(project_id))
    arguments = tuple(integer(value, field="identifier") for value in args)
    await run_in_threadpool(method, actor, integer(project_id, field="identifier"), integer(form.get("revision", "")), *arguments, **kwargs)
    return RedirectResponse(_base(project_id), status_code=303)


@router.get("", name="production_list")
def projects(request: Request):
    owner_objects(request)
    page = integer(request.query_params.get("page", "1"), field="page")
    archived = request.query_params.get("archived", "") == "1"
    listed, total = request.app.state.production_service.list_projects(page=page, archived=archived)
    with closing(open_readonly(request.app.state.settings.database_path)) as conn:
        owned = {row["id"] for row in conn.execute("SELECT id FROM production_projects WHERE actor_admin_id=?", (request.state.actor.admin_id,))}
        metadata = {row["id"]: dict(row) for row in conn.execute("""SELECT p.id,p.updated_at,
            (SELECT count(*) FROM production_chapters c WHERE c.project_id=p.id AND c.active=1 AND c.current_revision_id IS NOT NULL) AS completed,
            (SELECT count(*) FROM production_chapters c WHERE c.project_id=p.id AND c.active=1) AS chapter_total,
            (SELECT id FROM production_jobs j WHERE j.project_id=p.id AND j.state IN ('queued','running') ORDER BY id DESC LIMIT 1) AS active_job
            FROM production_projects p WHERE p.actor_admin_id=?""", (request.state.actor.admin_id,))}
    for item in metadata.values():
        item["updated_at"] = _local_time(item["updated_at"])
    return render(request, "production/projects.html", projects=[p for p in listed if p.id in owned], total=total, page=page,
        archived=archived, metadata=metadata)


@router.get("/new", name="production_new")
def new(request: Request):
    return render(request, "production/project.html", project=None, values={"chapter_count": 10, "mode": "quick"},
        settings=request.app.state.production_settings, categories=[])


@router.post("/new", name="production_create")
async def create(request: Request):
    actor, form = await protected_post(request, reload_url="/admin/production/new")
    def save():
        return request.app.state.production_service.create(actor, _brief(form), mode=form.get("mode", "quick"))
    project = await run_in_threadpool(save)
    return RedirectResponse(_base(project.id), status_code=303)


@router.get("/{project_id}", name="production_project")
def project(request: Request, project_id: str):
    return render(request, "production/project.html", **_data(request, integer(project_id, field="identifier")))


@router.post("/{project_id}/brief", name="production_brief")
async def brief(request: Request, project_id: str):
    actor, form = await protected_post(request, reload_url=_base(project_id))
    await run_in_threadpool(lambda: request.app.state.production_service.update(actor, integer(project_id, field="identifier"),
        integer(form.get("revision", "")), _brief(form)))
    return RedirectResponse(_base(project_id), status_code=303)


@router.post("/{project_id}/mode", name="production_mode")
async def mode(request: Request, project_id: str):
    actor, form = await protected_post(request, reload_url=_base(project_id))
    await run_in_threadpool(request.app.state.production_service.set_mode, actor, integer(project_id, field="identifier"),
        integer(form.get("revision", "")), form.get("mode", ""))
    return RedirectResponse(_base(project_id), status_code=303)


@router.post("/{project_id}/archive", name="production_archive")
async def archive(request: Request, project_id: str):
    return await _write(request, project_id, request.app.state.production_service.archive)


@router.post("/{project_id}/references/save", name="production_reference_save")
async def reference_save(request: Request, project_id: str):
    actor, form = await protected_post(request, reload_url=_base(project_id))
    await run_in_threadpool(lambda: request.app.state.production_service.save_reference(actor, integer(project_id, field="identifier"),
        integer(form.get("revision", "")), integer(form.get("reference_id", ""), field="reference_id", optional=True),
        ReferenceInput(text=form.get("text", ""), source_note=form.get("source_note", ""), source_status=form.get("source_status", "unprovided"))))
    return RedirectResponse(_base(project_id), status_code=303)


@router.post("/{project_id}/references/{reference_id}/remove", name="production_reference_remove")
async def reference_remove(request: Request, project_id: str, reference_id: str):
    return await _write(request, project_id, request.app.state.production_service.remove_reference, reference_id)


@router.post("/{project_id}/outline/save", name="production_outline_save")
async def outline_save(request: Request, project_id: str):
    actor, form = await protected_post(request, reload_url=_base(project_id))
    await run_in_threadpool(lambda: request.app.state.draft_service.save_outline(actor, integer(project_id, field="identifier"),
        integer(form.get("revision", "")), OutlineInput(items=json_value(form.get("outline_json", ""), field="outline_json"))))
    return RedirectResponse(_base(project_id), status_code=303)


@router.post("/{project_id}/outline/confirm", name="production_outline_confirm")
async def outline_confirm(request: Request, project_id: str):
    return await _write(request, project_id, request.app.state.draft_service.confirm_outline)


@router.post("/{project_id}/outline/adopt", name="production_outline_adopt")
async def outline_adopt(request: Request, project_id: str):
    actor, form = await protected_post(request, reload_url=_base(project_id))
    def save():
        mapping = json_value(form.get("matches_json", ""), field="matches_json")
        if not isinstance(mapping, dict):
            raise BusinessError("invalid_production", "请明确填写每个候选位置的新旧章节映射。", 400)
        matches = {integer(key, field="position"): value for key, value in mapping.items()}
        if len(matches) != len(mapping):
            raise BusinessError("invalid_production", "章节映射位置不能重复。", 400)
        return request.app.state.draft_service.adopt_outline(actor, integer(project_id, field="identifier"), integer(form.get("revision", "")),
            integer(form.get("candidate_id", ""), field="candidate_id"), matches)
    await run_in_threadpool(save)
    return RedirectResponse(_base(project_id), status_code=303)


@router.get("/{project_id}/chapters/{chapter_id}", name="production_chapter")
def chapter(request: Request, project_id: str, chapter_id: str):
    data = _data(request, integer(project_id, field="identifier"))
    selected = next(c for c in data["frozen"].chapters if c.id == chapter_id)
    return render(request, "production/editor.html", chapter=selected, preview=_preview(selected.markdown), **data)


@router.post("/{project_id}/chapters/{chapter_id}/save", name="production_chapter_save")
async def chapter_save(request: Request, project_id: str, chapter_id: str):
    actor, form = await protected_post(request, reload_url=_base(project_id) + "/chapters/" + chapter_id)
    await run_in_threadpool(request.app.state.draft_service.save_chapter, actor, integer(project_id, field="identifier"), chapter_id,
        integer(form.get("body_revision", ""), field="body_revision", optional=True), form.get("markdown", ""))
    return RedirectResponse(_base(project_id) + "/chapters/" + chapter_id, status_code=303)


async def _submit(request, project_id, *, chapter_id=None):
    actor, form = await protected_post(request, reload_url=_base(project_id))
    def submit():
        return request.app.state.job_service.submit(actor, JobRequest(kind="chapter_regenerate" if chapter_id else form.get("kind", ""),
            project_id=integer(project_id, field="identifier"), project_revision=integer(form.get("revision", "")),
            chapter_ids=(chapter_id,) if chapter_id else _selection(form.get("chapter_ids", ""))),
            request.app.state.production_settings.public, idempotency_key=form.get("idempotency_key", ""), reserved_tokens=0)
    job = await run_in_threadpool(submit)
    return RedirectResponse(f"/admin/jobs/{job.id}", status_code=303)


@router.post("/{project_id}/chapters/{chapter_id}/regenerate", name="production_chapter_regenerate")
async def chapter_regenerate(request: Request, project_id: str, chapter_id: str):
    return await _submit(request, project_id, chapter_id=chapter_id)


@router.post("/{project_id}/generate", name="production_generate")
async def generate(request: Request, project_id: str):
    return await _submit(request, project_id)


@router.get("/{project_id}/candidates/{candidate_id}", name="production_candidate")
def candidate(request: Request, project_id: str, candidate_id: str):
    data = _data(request, integer(project_id, field="identifier"))
    with closing(open_readonly(request.app.state.settings.database_path)) as conn:
        row = dict(conn.execute("SELECT * FROM production_candidates WHERE id=? AND project_id=?",
            (integer(candidate_id, field="candidate_id"), data["project"].id)).fetchone())
        if row["kind"] == "chapter":
            text = conn.execute("SELECT markdown FROM chapter_revisions WHERE id=? AND project_id=? AND chapter_id=?",
                (row["chapter_revision_id"], data["project"].id, row["chapter_id"])).fetchone()[0]
            # Read retained source only after project/candidate ownership gates.
            # Mutation routes retain DraftService's active/current-outline guard.
            historical = conn.execute("SELECT * FROM production_chapters WHERE id=? AND project_id=?",
                                      (row["chapter_id"], data["project"].id)).fetchone()
            if historical is None:
                raise BusinessError("chapter_missing", "候选的章节历史不存在。", 404)
            body = request.app.state.draft_service._body(conn, historical)
            chapter_active = bool(historical["active"] and any(c.id == body.id for c in data["frozen"].chapters))
            mapping = ""
        else:
            outline = request.app.state.draft_service._outline_view(conn, request.app.state.draft_service._row(conn, data["project"].id), row["outline_revision_id"])
            text = json.dumps([item.model_dump() for item in outline.items], ensure_ascii=False, indent=2)
            mapping = json.dumps({str(i): None for i in range(1, len(outline.items) + 1)}, ensure_ascii=False, indent=2)
            body = None
            chapter_active = False
    return render(request, "production/candidate.html", candidate=row, candidate_text=text, chapter=body,
        chapter_active=chapter_active, candidate_preview=_preview(text) if row["kind"] == "chapter" else None,
        current_preview=_preview(body.markdown) if body else None, matches_json=mapping, **data)


@router.post("/{project_id}/candidates/{candidate_id}/adopt", name="production_candidate_adopt")
async def candidate_adopt(request: Request, project_id: str, candidate_id: str):
    actor, form = await protected_post(request, reload_url=_base(project_id) + "/candidates/" + candidate_id)
    await run_in_threadpool(request.app.state.draft_service.adopt_candidate, actor, integer(project_id, field="identifier"),
        integer(form.get("revision", "")), integer(candidate_id, field="candidate_id"),
        integer(form.get("body_revision", ""), field="body_revision", optional=True))
    return RedirectResponse(_base(project_id), status_code=303)


@router.post("/{project_id}/candidates/{candidate_id}/reject", name="production_candidate_reject")
async def candidate_reject(request: Request, project_id: str, candidate_id: str):
    return await _write(request, project_id, request.app.state.draft_service.reject_candidate, candidate_id)


@router.post("/{project_id}/acknowledge", name="production_acknowledge")
async def acknowledge(request: Request, project_id: str):
    actor, form = await protected_post(request, reload_url=_base(project_id))
    await run_in_threadpool(request.app.state.draft_service.acknowledge_changes, actor, integer(project_id, field="identifier"),
        integer(form.get("revision", "")), _selection(form.get("chapter_ids", "")))
    return RedirectResponse(_base(project_id), status_code=303)


@router.get("/{project_id}/preview", name="production_preview")
def preview(request: Request, project_id: str):
    data = _data(request, integer(project_id, field="identifier"))
    return render(request, "production/preview.html", rendered=[(c, _preview(c.markdown)) for c in data["frozen"].chapters], **data)
