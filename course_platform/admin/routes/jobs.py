"""Private task pages, explicit maintenance and queued model diagnostics."""
from __future__ import annotations

import json
from contextlib import closing
from decimal import Decimal

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from ...audit import AuditEvent, append_event
from ...database import open_readonly, transaction
from ...domain import BusinessError
from ...jobs.models import JobRequest
from ...production.llm import Usage
from ...production.prompts import build_execution_plan
from .production_forms import ProductionRoute, integer, owner_objects, protected_post, render

router = APIRouter(prefix="/admin/jobs", route_class=ProductionRoute)
STOPPED = ("failed", "cancelled", "interrupted")


def readonly_job_metadata(request: Request, job_id: int) -> dict:
    """Authorized advisory UI only. Service CAS recomputes all final admission.

    The four-field RecoveryPreview stays unchanged. Reads expose no prompt,
    private source JSON, configured key, attempt token or raw SDK response.
    """
    owner_objects(request)
    state = request.app.state
    with closing(open_readonly(state.settings.database_path)) as conn:
        conn.execute("BEGIN")
        row = conn.execute("SELECT * FROM production_jobs WHERE id=?", (job_id,)).fetchone()
        job = {key: row[key] for key in ("id", "kind", "project_id", "parent_job_id", "state", "revision", "reason_code", "cancel_requested", "created_at", "started_at", "finished_at")}
        children = [dict(r) for r in conn.execute("SELECT id,state FROM production_jobs WHERE parent_job_id=? AND actor_admin_id=? ORDER BY id", (job_id, request.state.actor.admin_id))]
        attempts = [dict(r) for r in conn.execute("SELECT attempt_number,state,heartbeat_at,lease_until,started_at,finished_at FROM job_attempts WHERE job_id=? ORDER BY attempt_number", (job_id,))]
        steps = [dict(r) for r in conn.execute("SELECT step_number,kind,chapter_id,state,reason_code,candidate_id,outline_revision_id FROM job_steps WHERE job_id=? ORDER BY step_number", (job_id,))]
        calls = [dict(r) for r in conn.execute("""SELECT c.id,c.status,c.model,c.provider_request_id,c.error_code,c.input_tokens,c.output_tokens,c.reserved_tokens,
            c.price_json,c.started_at,c.finished_at,r.used_tokens,r.released_tokens,r.state AS accounting_state
            FROM model_calls c LEFT JOIN token_reservations r ON r.call_id=c.id AND r.job_id=c.job_id
            WHERE c.job_id=? ORDER BY c.id""", (job_id,))]
        history = [dict(r) for r in conn.execute("SELECT call_id,input_tokens,output_tokens,accept_retry_risk,created_at FROM job_requests WHERE job_id=? AND action='reconcile' ORDER BY id", (job_id,))]
        original = json.loads(row["input_json"])
    unknown_exposure = 0
    for call in calls:
        call["resolved"] = call["input_tokens"] is not None and call["used_tokens"] == call["input_tokens"] + call["output_tokens"]
        call["risk_accepted"] = any(r["call_id"] == call["id"] and r["accept_retry_risk"] for r in history)
        call["amount"] = None
        if call["status"] == "unknown" and not call["resolved"]:
            unknown_exposure += call["reserved_tokens"] - (call["released_tokens"] or 0)
        if call["price_json"] and call["resolved"]:
            price = json.loads(call["price_json"])
            call["amount"] = str((Decimal(price["input_per_million"]) * call["input_tokens"] + Decimal(price["output_per_million"]) * call["output_tokens"]) / Decimal(1000000))
            call["currency"], call["price_date"] = price["currency"], price["as_of"]
        del call["price_json"]
    preview = None
    recovery_error = None
    fresh_reserve = None
    child_kind = None
    candidate_only = False
    can_resume = False
    try:
        preview = state.recovery_job_service.recovery_preview(job_id)
        succeeded = {(s["kind"], s["chapter_id"]) for s in steps if s["state"] == "succeeded"}
        candidate_only = (job["kind"] == "course_draft_generate" and original.get("outline") is None
            and any(s["kind"] == "outline" and s["state"] == "succeeded" and s["candidate_id"] is not None for s in steps))
        if preview.pending_chapter_ids:
            child_kind = "chapter_regenerate" if job["kind"] == "chapter_regenerate" else "chapters_generate"
        elif job["kind"] in ("chapters_generate", "draft_validate") and ("validate", None) not in succeeded:
            child_kind = "draft_validate"
        elif job["kind"] == "course_draft_generate":
            if ("outline", None) not in succeeded:
                child_kind = "course_draft_generate"
            elif original.get("outline") is None and not candidate_only and ("validate", None) not in succeeded:
                child_kind = "draft_validate"
        elif job["kind"] == "outline_generate" and ("outline", None) not in succeeded:
            child_kind = "outline_generate"
        elif job["kind"] == "connection_test" and ("connection_test", None) not in succeeded:
            child_kind = "connection_test"
        can_resume = bool(job["state"] in STOPPED and not children and preview.unchanged_input and not candidate_only and child_kind)
        if can_resume:
            frozen = state.draft_service.freeze_input(job["project_id"]) if job["project_id"] is not None else None
            plan = build_execution_plan(frozen, kind=child_kind, chapter_ids=preview.pending_chapter_ids,
                config=state.production_settings.public)
            fresh_reserve = sum(state.token_budget.estimate(item) for item in plan)
    except BusinessError as error:
        recovery_error = error
        can_resume = False
    return dict(job=job, children=children, attempts=attempts, steps=steps, calls=calls, reconciliation_history=history,
        recovery=preview, recovery_error=recovery_error, candidate_only=candidate_only, can_resume=can_resume,
        fresh_reserve=fresh_reserve, unknown_exposure=unknown_exposure, child_kind=child_kind)


@router.get("", name="job_list")
def jobs(request: Request):
    owner_objects(request)
    page = integer(request.query_params.get("page", "1"), field="page")
    if page > 9223372036854775807 // 20:
        raise BusinessError("invalid_page", "分页参数无效。", 400)
    with closing(open_readonly(request.app.state.settings.database_path)) as conn:
        listed = [dict(row) for row in conn.execute("SELECT id,project_id,kind,state,reason_code,created_at FROM production_jobs WHERE actor_admin_id=? ORDER BY id DESC LIMIT 20 OFFSET ?",
            (request.state.actor.admin_id, (page - 1) * 20))]
        heartbeats = [dict(row) for row in conn.execute("SELECT heartbeat_at,job_id FROM worker_heartbeats ORDER BY heartbeat_at DESC")]
        last_check = conn.execute("""SELECT created_at FROM admin_events WHERE action='job.interrupt'
            AND object_type='job' AND object_id='maintenance' AND reason='maintenance_checked'
            AND outcome='success' ORDER BY id DESC LIMIT 1""").fetchone()
    return render(request, "jobs/list.html", jobs=listed, page=page, heartbeats=heartbeats,
        last_maintenance=last_check["created_at"] if last_check else None)


# Static model routes precede /{job_id}; /model is never parsed as a job ID.
@router.get("/model", name="job_model")
def model(request: Request):
    return render(request, "jobs/detail.html", model_diagnostic=True, settings=request.app.state.production_settings)


@router.post("/model/test-connection", name="job_test_connection")
async def test_connection(request: Request):
    actor, form = await protected_post(request, reload_url="/admin/jobs/model")
    job = await run_in_threadpool(request.app.state.job_service.submit, actor, JobRequest(kind="connection_test", project_id=None, project_revision=None),
        request.app.state.production_settings.public, idempotency_key=form.get("idempotency_key", ""), reserved_tokens=0)
    return RedirectResponse(f"/admin/jobs/{job.id}", status_code=303)


@router.post("/check-interrupted", name="job_check_interrupted")
async def check_interrupted(request: Request):
    actor, _ = await protected_post(request, reload_url="/admin/jobs")
    from ...jobs.worker import maintain
    recovery = request.app.state.recovery_job_service
    await run_in_threadpool(maintain, recovery.repository, request.app.state.token_budget)
    def record_check():
        # Coordinator repairs are already committed. Failure of this separate
        # marker transaction leaves those safe repairs intact and no new time.
        with transaction(request.app.state.settings.database_path, immediate=True) as conn:
            request.app.state.job_service._owner(conn, actor)
            append_event(conn, AuditEvent(actor.admin_id, "job", "maintenance", "job.interrupt",
                "maintenance_checked", "success", request.state.request_id, {}))
    await run_in_threadpool(record_check)
    return RedirectResponse("/admin/jobs", status_code=303)


@router.get("/{job_id:int}", name="job_detail")
def detail(request: Request, job_id: int):
    return render(request, "jobs/detail.html", model_diagnostic=False,
        **readonly_job_metadata(request, integer(job_id, field="identifier")))


@router.post("/{job_id}/cancel", name="job_cancel")
async def cancel(request: Request, job_id: str):
    actor, form = await protected_post(request, reload_url=f"/admin/jobs/{job_id}")
    await run_in_threadpool(request.app.state.recovery_job_service.cancel, actor, integer(job_id, field="identifier"), integer(form.get("revision", "")))
    return RedirectResponse(f"/admin/jobs/{job_id}", status_code=303)


@router.post("/{job_id}/resume", name="job_resume")
async def resume(request: Request, job_id: str):
    actor, form = await protected_post(request, reload_url=f"/admin/jobs/{job_id}")
    child = await run_in_threadpool(request.app.state.recovery_job_service.resume, actor, integer(job_id, field="identifier"),
        integer(form.get("revision", "")), request.app.state.production_settings.public,
        idempotency_key=form.get("idempotency_key", ""), reserved_tokens=0, confirm=form.get("confirm", "") == "yes")
    return RedirectResponse(f"/admin/jobs/{child.id}", status_code=303)


@router.post("/{job_id}/calls/{call_id}/reconcile", name="job_reconcile")
async def reconcile(request: Request, job_id: str, call_id: str):
    actor, form = await protected_post(request, reload_url=f"/admin/jobs/{job_id}")
    def save():
        usage = None
        if form.get("input_tokens", "") or form.get("output_tokens", ""):
            # Known usage permits zero; other IDs/revisions do not.
            def tokens(name):
                value = form.get(name, "")
                if value == "0":
                    return 0
                return integer(value, field=name)
            usage = Usage(tokens("input_tokens"), tokens("output_tokens"))
        request.app.state.recovery_job_service.reconcile(actor, integer(job_id, field="identifier"), integer(call_id, field="identifier"),
            integer(form.get("revision", "")), usage=usage, accept_retry_risk=form.get("accept_retry_risk", "") == "yes")
    await run_in_threadpool(save)
    return RedirectResponse(f"/admin/jobs/{job_id}", status_code=303)
