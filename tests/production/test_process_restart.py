"""Actual Web/worker process restart, local supplier receipts and durable fences."""

import json
import re
from contextlib import contextmanager

import httpx
import pytest

from course_platform.database import transaction
from course_platform.domain import BusinessError
from course_platform.jobs.models import Claim, JobRequest
from course_platform.jobs.repository import JobRepository
from course_platform.production.models import BriefInput, OutlineInput, OutlineItem
from tests.jobs.fakes import PROCESS_BODY, ProcessHarness, wait_until


@pytest.fixture
def processes(tmp_path, db_path, clock, product_owner):
    harness = ProcessHarness(tmp_path, db_path, product_owner, clock)
    try:
        yield harness
    finally:
        harness.close()


def prepared(h, *, kind="chapters_generate", bodies=False):
    p = h.projects.create(h.actor, BriefInput(title="task12-private-course", chapter_count=2), mode="quick")
    o = h.drafts.save_outline(h.actor, p.id, p.revision, OutlineInput(items=[
        OutlineItem(chapter_id=None, title="第一章", goal="学习"),
        OutlineItem(chapter_id=None, title="第二章", goal="实践")]))
    if bodies:
        for item in o.items:
            h.drafts.save_chapter(h.actor, p.id, item.chapter_id, None, "task12-private-human-source")
    p = h.projects.get(p.id)
    ids = (o.items[0].chapter_id,) if kind == "chapter_regenerate" else ()
    j = h.jobs.submit(h.actor, JobRequest(kind=kind, project_id=p.id, project_revision=p.revision, chapter_ids=ids),
        h.config.public, idempotency_key=f"task12-{p.id}", reserved_tokens=0)
    return p, j, o.items[0].chapter_id


def ready_worker(h, supplier, *, direct=False):
    process = h.spawn("worker", supplier, direct=direct)
    assert wait_until(lambda: process.poll() is not None or "worker_ready" in process.task12_log.read_text(encoding="utf-8"))
    assert process.poll() is None, process.task12_log.read_text(encoding="utf-8")
    return process


def accepted(process, supplier, count=1, *, seconds=15):
    assert wait_until(lambda: len(supplier.requests) >= count or process.poll() is not None, seconds=seconds), (
        f"accepted={len(supplier.requests)}; "
        + process.task12_log.read_text(encoding="utf-8")[-6000:])
    assert len(supplier.requests) == count, process.task12_log.read_text(encoding="utf-8")
    assert supplier.requests[-1]["model"] == "deepseek-flash"
    assert supplier.requests[-1]["thinking"] == {"type": "disabled"}


def claim_of(h, job_id):
    with transaction(h.db) as conn:
        job = conn.execute("SELECT * FROM production_jobs WHERE id=?", (job_id,)).fetchone()
        attempt = conn.execute("SELECT * FROM job_attempts WHERE job_id=?", (job_id,)).fetchone()
        from course_platform.database import from_db_time
        return Claim(job_id, attempt["id"], attempt["attempt_token"], from_db_time(attempt["lease_until"]),
                     h.actor, JobRepository._input(conn, job), h.config.public)


@contextmanager
def login(h):
    client = httpx.Client(base_url=h.url, trust_env=False, follow_redirects=False, timeout=5)
    try:
        page = client.get("/admin/login")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        assert client.post("/admin/login", data={"username": "owner", "password": "example-pass-123", "csrf_token": csrf},
                           headers={"Origin": h.url}).status_code == 303
        yield client
    finally:
        client.close()


def form_fields(page, action):
    form = re.search(r'<form[^>]*action="' + re.escape(action) + r'"[^>]*>(.*?)</form>', page, re.S).group(1)
    return dict(re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form))


def test_real_web_restart_retains_queued_job_and_draft(processes):
    h = processes
    p, job, chapter = prepared(h, kind="chapter_regenerate", bodies=True)
    frozen = h.drafts.freeze_input(p.id)
    web = h.web()
    with login(h) as client:
        assert client.get(f"/admin/jobs/{job.id}").status_code == 200
        assert "task12-private-human-source" in client.get(f"/admin/production/{p.id}/chapters/{chapter}").text
    h.stop(web)
    web = h.web()
    with login(h) as client:
        page = client.get(f"/admin/jobs/{job.id}")
        assert page.status_code == 200 and "queued" in page.text
    assert h.jobs.get(job.id).state == "queued"
    assert h.drafts.freeze_input(p.id) == frozen
    assert h.rows("SELECT * FROM model_calls") == []
    assert h.rows("SELECT * FROM job_attempts") == []
    # Readiness describes Web independently of whether a worker exists.
    assert "web_ready worker=independent" in web.task12_log.read_text(encoding="utf-8")
    print("process-proof web_restart=2 queued=retained draft=exact calls=0 attempts=0")


def test_kill_worker_after_vendor_accept_does_not_replay_on_restart(processes):
    h = processes
    p, job, chapter = prepared(h, kind="chapter_regenerate", bodies=True)
    supplier = h.supplier([PROCESS_BODY])
    worker = ready_worker(h, supplier)
    accepted(worker, supplier)
    claim = claim_of(h, job.id)
    call = h.rows("SELECT * FROM model_calls")[0]
    assert call["status"] == "sent" and call["response_text"] is None
    reserve = h.rows("SELECT * FROM token_reservations WHERE call_id=?", (call["id"],))[0]
    h.stop(worker, kill=True)
    h.clock.advance(seconds=120)
    h.write_clock()
    restarted = ready_worker(h, supplier)
    assert wait_until(lambda: h.jobs.get(job.id).state == "interrupted")
    assert len(supplier.requests) == 1
    old = h.rows("SELECT * FROM model_calls WHERE id=?", (call["id"],))[0]
    assert old["status"] == "unknown" and old["input_tokens"] is None and old["output_tokens"] is None
    unknown_reserve = h.rows("SELECT * FROM token_reservations WHERE call_id=?", (call["id"],))[0]
    assert unknown_reserve["released_tokens"] == reserve["released_tokens"] == 0
    assert unknown_reserve["reserved_tokens"] == reserve["reserved_tokens"] == old["reserved_tokens"] > 0
    assert unknown_reserve["used_tokens"] is None and unknown_reserve["state"] == "unknown"
    assert h.rows("SELECT state FROM token_reservations WHERE call_id IS NULL AND job_id=?", (job.id,))[0]["state"] == "released"
    assert len(h.rows("SELECT * FROM job_attempts WHERE job_id=?", (job.id,))) == 1
    # Restart runs only a newly queued unrelated job; the lost attempt is retained.
    second = h.jobs.submit(h.actor, JobRequest(kind="connection_test", project_id=None, project_revision=None),
        h.config.public, idempotency_key="fresh-after-kill", reserved_tokens=0)
    assert wait_until(lambda: h.jobs.get(second.id).state == "succeeded")
    assert len(supplier.requests) == 2
    assert supplier.requests[-1]["max_tokens"] == 32
    assert h.jobs.get(job.id).state == "interrupted"
    assert h.rows("SELECT attempt_token FROM job_attempts WHERE job_id=?", (second.id,))[0]["attempt_token"] != claim.attempt_token
    assert h.rows("SELECT * FROM model_calls WHERE id=?", (call["id"],))[0] == old
    body = h.drafts.freeze_input(p.id).chapters[0]
    h.drafts.save_chapter(h.actor, p.id, chapter, body.body_revision, "人工重启后正文")
    late = h.spawn("late", supplier)
    assert wait_until(lambda: late.poll() is not None)
    assert late.returncode == 0, late.task12_log.read_text(encoding="utf-8")
    proof = json.loads(late.task12_log.read_text(encoding="utf-8"))
    assert proof["fence"] == "attempt_expired" and proof["human_unchanged"] is True
    after = h.rows("SELECT * FROM model_calls WHERE id=?", (call["id"],))[0]
    assert after["status"] == "unknown" and after["response_disposition"] == "isolated"
    assert after["input_tokens"] == 12 and after["output_tokens"] == 4
    assert h.drafts.freeze_input(p.id).chapters[0].markdown == "人工重启后正文"
    assert len(supplier.requests) == 2
    assert h.rows("SELECT * FROM production_candidates") == []
    assert claim.attempt_token == h.rows("SELECT attempt_token FROM job_attempts WHERE id=?", (claim.attempt_id,))[0]["attempt_token"]
    h.signal(restarted)
    assert wait_until(lambda: restarted.poll() is not None)
    assert restarted.returncode == 0
    print("process-proof kill=after_http_accept count_before=1 count_after_restart=1 fresh_job_count=2 original=interrupted unknown_reserve=retained old_token=expired late=isolated human=unchanged")


def test_maintenance_fences_after_60_and_marks_after_120(processes):
    h = processes
    _, job, _ = prepared(h)
    supplier = h.supplier([PROCESS_BODY])
    worker = ready_worker(h, supplier)
    accepted(worker, supplier)
    claim = claim_of(h, job.id)
    h.stop(worker, kill=True)
    h.clock.advance(seconds=60)
    h.write_clock()
    restarted = ready_worker(h, supplier)
    repo = JobRepository(h.db, clock=h.clock.now)
    with pytest.raises(BusinessError) as denied:
        with transaction(h.db, immediate=True) as conn:
            repo.require_attempt_in_tx(conn, claim, h.clock.now())
    assert denied.value.code == "attempt_expired"
    with pytest.raises(BusinessError) as heartbeat:
        repo.heartbeat(claim)
    assert heartbeat.value.code == "attempt_expired"
    assert h.jobs.get(job.id).state == "running"
    assert h.rows("SELECT status FROM model_calls")[0]["status"] == "sent"
    h.clock.advance(seconds=59)
    h.write_clock()
    assert h.jobs.get(job.id).state == "running"
    h.clock.advance(seconds=1)
    h.write_clock()
    assert wait_until(lambda: h.jobs.get(job.id).state == "interrupted")
    assert h.rows("SELECT status,input_tokens FROM model_calls")[0] == {"status": "unknown", "input_tokens": None}
    assert len(supplier.requests) == 1
    assert h.rows("SELECT job_id,attempt_id FROM worker_heartbeats")[0] == {"job_id": None, "attempt_id": None}
    h.signal(restarted)
    assert wait_until(lambda: restarted.poll() is not None)
    print("process-proof lease60=write_and_heartbeat_denied t119=running t120=interrupted supplier_count=1 unknown_usage=null heartbeat_pointer=null")


@pytest.mark.parametrize("signal_name,direct", [("SIGINT", True), ("SIGTERM", True),
                                               ("SIGINT", False), ("SIGTERM", False)])
def test_signal_stops_next_request_inside_running_job_and_keeps_inflight_result(processes, signal_name, direct):
    h = processes
    p, job, _ = prepared(h)
    supplier = h.supplier([PROCESS_BODY, PROCESS_BODY])
    # Direct cases established the genuine missing stop RED. CLI cases also
    # exercise the actual production signal registration and fixed entrypoint.
    process = ready_worker(h, supplier, direct=direct)
    accepted(process, supplier)
    signal_proof = h.signal(process, signal_name)
    supplier.release.set()
    assert wait_until(lambda: process.poll() is not None)
    assert process.returncode == 0, process.task12_log.read_text(encoding="utf-8")
    assert len(supplier.requests) == 1, "Stop must prevent the next request in the same job"
    calls = h.rows("SELECT status,input_tokens,output_tokens FROM model_calls")
    assert calls == [{"status": "successful", "input_tokens": 12, "output_tokens": 4}]
    draft = h.drafts.freeze_input(p.id)
    assert draft.chapters[0].markdown == PROCESS_BODY and draft.chapters[1].markdown == ""
    assert h.jobs.get(job.id).state == "failed" and h.jobs.get(job.id).reason_code == "worker_stopped"
    assert h.rows("SELECT job_id,attempt_id FROM worker_heartbeats")[0] == {"job_id": None, "attempt_id": None}
    assert len(h.rows("SELECT * FROM job_steps")) == 1
    print(f"process-proof signal={signal_proof} accepted_before=1 accepted_after=1 known_usage=12+4 first_body=retained second=empty fabricated_sent=0 heartbeat=clean")


def test_sigint_preserves_completed_last_inflight_goal_and_cleans_heartbeat(processes):
    h = processes
    job = h.jobs.submit(h.actor, JobRequest(kind="connection_test", project_id=None, project_revision=None),
        h.config.public, idempotency_key="last-inflight", reserved_tokens=0)
    supplier = h.supplier(["连接完成"])
    process = ready_worker(h, supplier)
    accepted(process, supplier)
    h.signal(process)
    supplier.release.set()
    assert wait_until(lambda: process.poll() is not None)
    assert process.returncode == 0
    assert h.jobs.get(job.id).state == "succeeded", "A complete single-request goal should retain success"
    assert h.rows("SELECT status,input_tokens,output_tokens FROM model_calls") == [
        {"status": "successful", "input_tokens": 12, "output_tokens": 4}]
    assert h.rows("SELECT state FROM job_steps") == [{"state": "succeeded"}]
    assert h.rows("SELECT job_id,attempt_id FROM worker_heartbeats")[0] == {"job_id": None, "attempt_id": None}
    assert len(supplier.requests) == 1
    print("process-proof final_inflight=successful job=succeeded supplier_count=1 usage=12+4 heartbeat=clean")


def test_actual_worker_heartbeats_while_supplier_is_waiting(processes):
    from datetime import timedelta
    from course_platform.database import from_db_time

    h = processes
    _, job, _ = prepared(h)
    supplier = h.supplier([PROCESS_BODY])
    process = ready_worker(h, supplier)
    accepted(process, supplier)
    started = h.rows("SELECT * FROM job_attempts WHERE job_id=?", (job.id,))[0]
    h.clock.advance(seconds=10)
    h.write_clock()
    assert wait_until(lambda: h.rows("SELECT heartbeat_at FROM job_attempts WHERE job_id=?", (job.id,))[0]["heartbeat_at"] != started["heartbeat_at"], seconds=12)
    current = h.rows("SELECT * FROM job_attempts WHERE job_id=?", (job.id,))[0]
    assert from_db_time(current["heartbeat_at"]) == h.clock.now()
    assert from_db_time(current["lease_until"]) == h.clock.now() + timedelta(seconds=60)
    assert h.rows("SELECT status,response_text FROM model_calls") == [{"status": "sent", "response_text": None}]
    h.signal(process)
    supplier.release.set()
    assert wait_until(lambda: process.poll() is not None)
    assert process.returncode == 0 and len(supplier.requests) == 1
    assert h.rows("SELECT job_id,attempt_id FROM worker_heartbeats")[0] == {"job_id": None, "attempt_id": None}
    retained = h.rows("SELECT * FROM job_attempts WHERE job_id=?", (job.id,))[0]
    h.clock.advance(seconds=10)
    h.write_clock()
    assert h.rows("SELECT * FROM job_attempts WHERE job_id=?", (job.id,))[0] == retained
    print("process-proof heartbeat=t10_while_http_pending lease=clock+60 stop=awaited pointer=null post_exit_write=0")


def test_unconfigured_worker_is_safe_and_actual_web_stays_available(processes):
    h = processes
    _, job, _ = prepared(h)
    h.env["DEEPSEEK_API_KEY"] = ""
    h.env["COURSE_MODEL_CONTEXT_TOKENS"] = ""
    supplier = h.supplier([])
    worker = ready_worker(h, supplier)
    assert wait_until(lambda: h.jobs.get(job.id).state == "failed")
    diagnostic = worker.task12_log.read_text(encoding="utf-8")
    assert "model_context_missing" in diagnostic
    assert "task12-fake-key" not in diagnostic
    web = h.web()
    with login(h) as client:
        assert client.get("/help").status_code == 200
        page = client.get("/admin/jobs/model")
        assert page.status_code == 200 and "密钥未配置" in page.text
        assert client.get(f"/admin/jobs/{job.id}").status_code == 200
    assert h.jobs.get(job.id).reason_code == "model_config_changed"
    assert h.rows("SELECT * FROM model_calls") == []
    assert len(supplier.requests) == 0
    h.signal(worker)
    assert wait_until(lambda: worker.poll() is not None)
    assert worker.returncode == 0
    h.stop(web)
    print("process-proof unconfigured_worker=local_only actual_web=M1+admin_ready model_job=configuration_blocked supplier_count=0")


def test_cli_local_validation_runs_without_context_or_key_and_model_job_never_sends(processes):
    from dataclasses import replace

    h = processes
    project, model_job, _ = prepared(h)
    h.env["DEEPSEEK_API_KEY"] = ""
    h.env["COURSE_MODEL_CONTEXT_TOKENS"] = ""
    h.config = replace(h.config, api_key="", public=h.config.public.model_copy(update={"context_tokens": None}))
    h.jobs.configure_execution(h.config, h.budget)
    local = h.jobs.submit(h.actor, JobRequest(kind="draft_validate", project_id=project.id,
        project_revision=h.projects.get(project.id).revision), h.config.public,
        idempotency_key="local-without-model", reserved_tokens=0)
    reservations = h.rows("SELECT id,call_id,reserved_tokens FROM token_reservations ORDER BY id")
    supplier = h.supplier([])
    process = ready_worker(h, supplier)
    assert wait_until(lambda: h.jobs.get(local.id).state == "succeeded")
    assert h.jobs.get(model_job.id).state == "failed"
    assert h.jobs.get(model_job.id).reason_code == "model_config_changed"
    assert h.rows("SELECT id,call_id,reserved_tokens FROM token_reservations ORDER BY id") == reservations
    assert h.rows("SELECT * FROM model_calls") == []
    assert h.rows("SELECT human_review_required FROM draft_checks") == [{"human_review_required": 1}]
    assert len(supplier.requests) == 0
    assert '"status": "local_only"' in process.task12_log.read_text(encoding="utf-8")
    h.signal(process)
    assert wait_until(lambda: process.poll() is not None)
    assert process.returncode == 0
    print("process-proof CLI_unconfigured=local_only draft_validate=succeeded model_job=configuration_blocked new_calls=0 new_reservations=0 supplier_count=0")


def test_stopped_worker_keeps_ui_cancel_and_recovery_available(processes):
    h = processes
    _, job, _ = prepared(h)
    supplier = h.supplier([PROCESS_BODY])
    worker = ready_worker(h, supplier)
    # Actual cold SDK imports exceeded15s in the retained timing diagnosis.
    # Bound this setup wait only; supplier receipts and runtime deadlines remain.
    accepted(worker, supplier, seconds=45)
    h.stop(worker, kill=True)
    web = h.web()
    with login(h) as client:
        def displayed_history():
            return {table: h.rows(f"SELECT * FROM {table} ORDER BY 1") for table in (
                "production_jobs", "job_attempts", "job_steps", "worker_heartbeats", "model_calls",
                "token_reservations", "job_requests", "admin_events", "production_projects", "chapter_revisions")}
        action = f"/admin/jobs/{job.id}/cancel"
        fresh_before = displayed_history()
        page = client.get(f"/admin/jobs/{job.id}")
        assert "执行异常：" not in page.text and displayed_history() == fresh_before
        fresh_page = page
        response = client.post(action, data=form_fields(page.text, action), headers={"Origin": h.url})
        assert response.status_code == 303
        assert h.rows("SELECT cancel_requested FROM production_jobs WHERE id=?", (job.id,))[0]["cancel_requested"] == 1
        assert len(supplier.requests) == 1
        h.clock.advance(seconds=120)
        h.write_clock()
        # A read never performs interruption/ledger maintenance.
        expired_before = displayed_history()
        page = client.get(f"/admin/jobs/{job.id}")
        assert page.status_code == 200 and h.jobs.get(job.id).state == "running"
        maintenance = client.get("/admin/jobs")
        assert maintenance.status_code == 200
        assert displayed_history() == expired_before and len(supplier.requests) == 1
        print("process-proof expired_UI_GET clock=+120 DB_running=retained all_task_ledger_audit_source_rows=exact supplier_count=1")
        assert 'data-worker-status="heartbeat_overdue"' in page.text
        assert "执行异常：心跳已达到 120 秒未更新" in page.text and "worker 可能失联" in page.text
        assert "任务记录仍为 running" in page.text and "正在执行；" not in page.text
        assert 'data-worker-status="heartbeat_overdue"' in maintenance.text
        assert "执行异常：心跳已达到 120 秒未更新" in maintenance.text and "检查中断状态" in maintenance.text
        assert 'data-worker-status="fresh"' in fresh_page.text and "执行租约有效" in fresh_page.text
        action = "/admin/jobs/check-interrupted"
        assert client.post(action, data=form_fields(maintenance.text, action), headers={"Origin": h.url}).status_code == 303
        assert h.jobs.get(job.id).state == "interrupted"
        original = h.rows("SELECT * FROM model_calls")[0]
        reservation = h.rows("SELECT * FROM token_reservations WHERE call_id=?", (original["id"],))[0]
        page = client.get(f"/admin/jobs/{job.id}")
        action = f"/admin/jobs/{job.id}/calls/{original['id']}/reconcile"
        fields = form_fields(page.text, action)
        fields.update(accept_retry_risk="yes")
        assert client.post(action, data=fields, headers={"Origin": h.url}).status_code == 303
        page = client.get(f"/admin/jobs/{job.id}")
        action = f"/admin/jobs/{job.id}/resume"
        fields = form_fields(page.text, action)
        fields.update(confirm="yes")
        result = client.post(action, data=fields, headers={"Origin": h.url})
        assert result.status_code == 303
        children = h.rows("SELECT * FROM production_jobs WHERE parent_job_id=?", (job.id,))
        assert len(children) == 1 and children[0]["state"] == "queued"
        assert h.rows("SELECT * FROM model_calls WHERE id=?", (original["id"],))[0] == original
        assert h.rows("SELECT * FROM token_reservations WHERE call_id=?", (original["id"],))[0] == reservation
        assert len(supplier.requests) == 1
        assert page.headers["Cache-Control"] == "no-store"
    h.stop(web)
    print("process-proof stopped_ui=cancel+explicit_maintenance+unknown_risk+resume child=queued original_unknown_reserve=exact supplier_count=1 GET_mutation=0")
