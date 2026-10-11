"""Protected native production flows against the real app and SQLite."""
import asyncio
import html
import json
import re

import pytest

from course_platform.database import transaction
from course_platform.production.models import BriefInput, OutlineInput, OutlineItem


def csrf(client):
    return client.cookies.get("coursmith_admin_csrf")


def post(client, path, **data):
    return client.post(path, data={"csrf_token": csrf(client), **data}, headers={"Origin": "http://testserver"})


def textarea(response, name):
    return html.unescape(re.search(r'<textarea[^>]*name="' + name + r'"[^>]*>(.*?)</textarea>', response.text, re.S).group(1))


def test_large_chinese_markdown_survives_save_and_stale_conflict(production_data):
    d, client = production_data, production_data.client
    body = "中文😀百分号% <script>源码</script>\n" * 4000
    assert 65536 < len(body.encode()) <= 204800
    path = f"/admin/production/{d.project_id}/chapters/{d.chapter_id}/save"
    first = post(client, path, body_revision="", markdown=body)
    assert first.status_code == 303
    assert d.drafts.freeze_input(d.project_id).chapters[0].markdown == body
    stale = post(client, path, body_revision="", markdown=body + "本次未保存")
    assert stale.status_code == 409
    assert stale.headers["cache-control"] == "no-store"
    assert textarea(stale, "markdown") == body + "本次未保存"
    assert 'name="body_revision" value=""' in stale.text
    assert 'name="body_revision" value="1"' not in stale.text
    assert "<script>源码</script>" not in stale.text
    assert d.drafts.freeze_input(d.project_id).chapters[0].markdown == body


def test_factory_composes_services_without_worker_or_model_startup(production_client, monkeypatch):
    state = production_client.app.state
    for name in ("production_service", "draft_service", "job_service", "recovery_job_service", "production_settings", "token_budget"):
        assert hasattr(state, name), name
    import course_platform.jobs.worker as worker
    import course_platform.production.llm as llm
    def forbidden(*args, **kwargs):
        raise AssertionError("GET must not construct worker/model or maintain")
    monkeypatch.setattr(worker.Worker, "__init__", forbidden)
    monkeypatch.setattr(worker, "maintain", forbidden)
    monkeypatch.setattr(llm.OpenAIModel, "__init__", forbidden)
    for path in ("/admin/production", "/admin/production/new", "/admin/jobs", "/admin/jobs/model"):
        response = production_client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
    assert "deepseek-flash" in production_client.get("/admin/jobs/model").text
    assert "task9-fake-api-key" not in production_client.get("/admin/jobs/model").text


def test_title_only_creation_and_generation_queues_no_model(production_client, db_path):
    client = production_client
    response = post(client, "/admin/production/new", title="Agent 开发", mode="quick")
    assert response.status_code == 303
    path = response.headers["location"]
    page = client.get(path)
    assert "Agent 开发" in page.text and "200000" in page.text
    response = post(client, path + "/generate", revision="1", kind="course_draft_generate", idempotency_key="web-quick")
    assert response.status_code == 303 and response.headers["location"].startswith("/admin/jobs/")
    with transaction(db_path) as connection:
        row = connection.execute("SELECT * FROM production_jobs").fetchone()
        assert row["state"] == "queued" and json.loads(row["input_json"])["brief"]["chapter_count"] == 10
        assert connection.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM token_reservations").fetchone()[0] == 1
    assert "Agent 开发" not in client.get("/admin/jobs").text
    response = post(client, "/admin/jobs/model/test-connection", idempotency_key="web-connection", api_key="ignored", model="unrequested")
    assert response.status_code == 303
    with transaction(db_path) as connection:
        row = connection.execute("SELECT * FROM production_jobs WHERE kind='connection_test'").fetchone()
        assert json.loads(row["config_json"])["model"] == "deepseek-flash"
        assert connection.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 0


def test_project_reference_outline_and_chapter_native_actions(production_data):
    d, c = production_data, production_data.client
    base = f"/admin/production/{d.project_id}"
    response = post(c, base + "/brief", revision="2", title="更新课程", chapter_count="2", goal="目标")
    assert response.status_code == 303
    response = post(c, base + "/references/save", revision="3", text="私有资料", source_note="自有笔记", source_status="owner_noted")
    assert response.status_code == 303
    ref = d.drafts.freeze_input(d.project_id).references[0]
    response = post(c, base + f"/references/{ref.id}/remove", revision="4")
    assert response.status_code == 303 and not d.drafts.freeze_input(d.project_id).references
    response = post(c, base + "/mode", revision="5", mode="precise")
    assert response.status_code == 303
    outline = [{"chapter_id": d.chapter_id, "title": "新标题", "goal": "目标"}]
    assert post(c, base + "/outline/save", revision="6", outline_json=json.dumps(outline)).status_code == 303
    assert post(c, base + "/outline/confirm", revision="7").status_code == 303
    assert d.drafts.freeze_input(d.project_id).outline.confirmed
    assert post(c, base + "/acknowledge", revision="8", chapter_ids=d.chapter_id).status_code == 303
    assert post(c, base + "/chapters/" + d.chapter_id + "/save", body_revision="", markdown="# 源文").status_code == 303
    assert "源文" in c.get(base + "/preview").text
    assert post(c, base + "/archive", revision="10").status_code == 303
    assert d.projects.get(d.project_id).status == "archived"


def test_cross_project_candidate_and_chapter_id_rejected(production_data):
    from tests.production.test_drafts import generate
    d, c = production_data, production_data.client
    other = d.projects.create(d.actor, BriefInput(title="其他项目", chapter_count=1), mode="quick")
    outline = d.drafts.save_outline(d.actor, other.id, other.revision, OutlineInput(items=[OutlineItem(chapter_id=None, title="其他章", goal="")]))
    foreign = outline.items[0].chapter_id
    result = generate(d.db_path, d.clock, d.actor, d.drafts, other.id, foreign, None, "foreign-candidate-secret")
    base = f"/admin/production/{d.project_id}"
    assert c.get(f"/admin/production/{other.id}/chapters/{foreign}").status_code == 200
    assert c.get(f"/admin/production/{other.id}/candidates/{result.candidate_id}").status_code == 200
    for path in (base + f"/chapters/{foreign}", base + f"/candidates/{result.candidate_id}"):
        response = c.get(path)
        assert response.status_code == 404 and "foreign-candidate-secret" not in response.text
    for suffix in (f"/chapters/{foreign}/save", f"/chapters/{foreign}/regenerate", f"/candidates/{result.candidate_id}/adopt", f"/candidates/{result.candidate_id}/reject"):
        response = post(c, base + suffix, revision="2", body_revision="", markdown="input-must-not-echo")
        assert response.status_code == 404 and "input-must-not-echo" not in response.text


def test_removed_chapter_candidate_history_stays_readonly_and_owned(production_data):
    from tests.production.test_drafts import generate
    d, c = production_data, production_data.client
    result = generate(d.db_path, d.clock, d.actor, d.drafts, d.project_id,
                      d.chapter_id, None, "保留历史候选<script>历史源码</script>")
    base = f"/admin/production/{d.project_id}"
    url = base + f"/candidates/{result.candidate_id}"
    assert c.get(url).status_code == 200
    current = d.drafts.freeze_input(d.project_id)
    d.drafts.save_outline(d.actor, d.project_id, current.project_revision,
                         OutlineInput(items=[current.outline.items[1]]))
    listing = c.get(base)
    assert listing.status_code == 200 and url in listing.text
    history = c.get(url)
    assert history.status_code == 200 and history.headers["cache-control"] == "no-store"
    assert textarea(history, "candidate_text") == "保留历史候选<script>历史源码</script>"
    assert "章节已移出当前大纲" in history.text
    assert 'action="' + url + '/adopt"' not in history.text
    assert "<script>历史源码</script>" not in history.text
    before = d.drafts.freeze_input(d.project_id)
    assert post(c, url + "/adopt", revision=str(before.project_revision), body_revision="").status_code == 404
    assert d.drafts.freeze_input(d.project_id) == before
    other = d.projects.create(d.actor, BriefInput(title="另一项目"), mode="quick")
    denial = c.get(f"/admin/production/{other.id}/candidates/{result.candidate_id}")
    assert denial.status_code == 404 and "保留历史候选" not in denial.text


def test_job_timestamps_display_shanghai_without_changing_utc(production_data):
    d, c = production_data, production_data.client
    submitted = post(c, f"/admin/production/{d.project_id}/generate", revision="2",
                     kind="chapters_generate", idempotency_key="time-display")
    assert submitted.status_code == 303
    job_url = submitted.headers["location"]
    with transaction(d.db_path) as conn:
        before = tuple(conn.execute("SELECT * FROM production_jobs").fetchone())
    listed = c.get("/admin/jobs")
    assert listed.status_code == 200
    assert "上海时间" in listed.text and "2026-10-02 08:00:00" in listed.text
    detail = c.get(job_url)
    assert detail.status_code == 200 and "2026-10-02 08:00:00" in detail.text
    with transaction(d.db_path) as conn:
        assert tuple(conn.execute("SELECT * FROM production_jobs").fetchone()) == before


def test_cancel_resume_and_existing_child_navigation(production_data):
    d, c = production_data, production_data.client
    base = f"/admin/production/{d.project_id}"
    response = post(c, base + "/generate", revision="2", kind="chapters_generate", idempotency_key="resume-parent")
    assert response.status_code == 303
    job = response.headers["location"]
    assert post(c, job + "/cancel", revision="1").status_code == 303
    page = c.get(job)
    assert 'action="' + job + '/resume"' in page.text
    assert "新任务预留估算" in page.text and "原未知调用保留额度" in page.text
    child = post(c, job + "/resume", revision="2", confirm="yes", idempotency_key="resume-child")
    assert child.status_code == 303
    page = c.get(job)
    assert child.headers["location"] in page.text
    assert 'action="' + job + '/resume"' not in page.text
    denial = post(c, job + "/resume", revision="2", confirm="yes", idempotency_key="unsafe-branch")
    assert denial.status_code == 409 and "recovery_superseded" in denial.text


def test_missing_model_configuration_keeps_m1_and_local_pages_usable(admin_settings, monkeypatch):
    from fastapi.testclient import TestClient
    from course_platform.app import create_app
    monkeypatch.delenv("COURSE_MODEL_CONTEXT_TOKENS", raising=False)
    app = create_app(admin_settings)
    with TestClient(app, client=("198.51.100.7", 50000), follow_redirects=False) as c:
        assert c.get("/help").status_code == 200
        assert hasattr(app.state, "job_service")
        assert app.state.production_settings.public.context_tokens is None
        assert app.state.production_settings.api_key == ""


def post_login(client, token):
    return client.post("/admin/login", data={"username": "owner", "password": "example-pass-123", "csrf_token": token},
        headers={"Origin": "http://testserver"})


def run_controlled_worker(data, model):
    from course_platform.jobs.worker import Worker
    state = data.client.app.state
    worker = Worker(state.recovery_job_service.repository, state.draft_service, state.token_budget,
        model, state.production_settings, worker_id="task9-controlled", clock=data.clock.now)
    return worker


def test_factory_lifespan_and_get_never_construct_model_or_worker(admin_settings, monkeypatch):
    from fastapi.testclient import TestClient
    from course_platform.app import create_app
    from course_platform.jobs import worker
    from course_platform.production import llm
    def forbidden(*args, **kwargs):
        raise AssertionError("factory/lifespan must not start worker, model or maintenance")
    monkeypatch.setattr(worker.Worker, "__init__", forbidden)
    monkeypatch.setattr(worker, "maintain", forbidden)
    monkeypatch.setattr(llm.OpenAIModel, "__init__", forbidden)
    app = create_app(admin_settings)
    with TestClient(app, client=("198.51.100.7", 50000), follow_redirects=False) as c:
        assert c.get("/help").status_code == 200
        app.state.admin_service.initialize_owner("owner", "example-pass-123")
        challenge = c.get("/admin/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', challenge.text).group(1)
        assert post_login(c, token).status_code == 303
        assert c.get("/admin/production").status_code == 200
        assert app.state.job_service._execution_config.api_key == ""
        assert app.state.job_service._budget is app.state.token_budget


def test_known_usage_on_historical_unknown_and_fresh_advisory_reserve(production_data):
    from course_platform.production.llm import ModelFailure
    from tests.jobs.fakes import ScriptedModel
    from tests.jobs.test_worker import result
    d, c = production_data, production_data.client
    queued = post(c, f"/admin/production/{d.project_id}/generate", revision="2", kind="chapters_generate", idempotency_key="unknown-web")
    assert queued.status_code == 303
    model = ScriptedModel([result(), ModelFailure("response_unknown", None)])
    assert asyncio.run(run_controlled_worker(d, model).run_once())
    job_path = queued.headers["location"]
    page = c.get(job_path)
    assert page.status_code == 200 and "已成功章节：1；待生成章节：1" in page.text
    fresh = int(re.search(r'新任务预留估算：(\d+)', page.text).group(1))
    with transaction(d.db_path) as conn:
        call = dict(conn.execute("SELECT * FROM model_calls WHERE status='unknown'").fetchone())
        job = dict(conn.execute("SELECT * FROM production_jobs WHERE id=?", (call["job_id"],)).fetchone())
        original_reserve = dict(conn.execute("SELECT * FROM token_reservations WHERE call_id=?", (call["id"],)).fetchone())
    response = post(c, job_path + f"/calls/{call['id']}/reconcile", revision=str(job["revision"]), input_tokens="7", output_tokens="3")
    assert response.status_code == 303
    page = c.get(job_path)
    assert "已核对用量：输入 7，输出 3 token" in page.text
    assert "原未知调用保留额度：0 token" in page.text
    with transaction(d.db_path) as conn:
        assert conn.execute("SELECT status FROM model_calls WHERE id=?", (call["id"],)).fetchone()[0] == "unknown"
        reserve = dict(conn.execute("SELECT * FROM token_reservations WHERE call_id=?", (call["id"],)).fetchone())
        assert reserve["reserved_tokens"] == original_reserve["reserved_tokens"] and reserve["used_tokens"] == 10
        revision = conn.execute("SELECT revision FROM production_jobs WHERE id=?", (call["job_id"],)).fetchone()[0]
    child = post(c, job_path + "/resume", revision=str(revision), confirm="yes", idempotency_key="known-recovery")
    assert child.status_code == 303 and len(model.calls) == 2
    child_id = int(child.headers["location"].split("/")[-1])
    with transaction(d.db_path) as conn:
        selected = json.loads(conn.execute("SELECT chapter_ids_json FROM production_jobs WHERE id=?", (child_id,)).fetchone()[0])
        assert selected == [d.drafts.freeze_input(d.project_id).chapters[1].id]
        assert conn.execute("SELECT reserved_tokens FROM token_reservations WHERE job_id=? AND call_id IS NULL", (child_id,)).fetchone()[0] == fresh


def test_candidate_only_initial_outline_has_adopt_new_submission_guidance(production_data):
    from tests.jobs.fakes import ScriptedModel
    from tests.jobs.test_worker import outline
    d, c = production_data, production_data.client
    p = d.projects.create(d.actor, BriefInput(title="首次快速", chapter_count=2), mode="quick")
    queued = post(c, f"/admin/production/{p.id}/generate", revision="1", kind="course_draft_generate", idempotency_key="candidate-outline")
    assert queued.status_code == 303
    job_path = queued.headers["location"]
    async def run():
        gate = asyncio.Event()
        model = ScriptedModel([outline(2)], barrier=gate)
        task = asyncio.create_task(run_controlled_worker(d, model).run_once())
        await asyncio.wait_for(model.sent.wait(), 5)
        response = await asyncio.to_thread(post, c, job_path + "/cancel", revision="2")
        assert response.status_code == 303
        gate.set()
        assert await asyncio.wait_for(task, 5)
        return len(model.calls)
    assert asyncio.run(run()) == 1
    page = c.get(job_path)
    assert page.status_code == 200
    assert "请人工采用大纲并提交新的制作任务" in page.text
    assert 'action="' + job_path + '/resume"' not in page.text
    revision = c.app.state.job_service.get(int(job_path.split("/")[-1])).revision
    denial = post(c, job_path + "/resume", revision=str(revision), confirm="yes", idempotency_key="unsafe-outline-resume")
    assert denial.status_code == 409 and "recovery_candidate_pending" in denial.text
    with transaction(d.db_path) as conn:
        candidate = conn.execute("SELECT id FROM production_candidates WHERE project_id=? AND kind='outline'", (p.id,)).fetchone()[0]
    adoption = post(c, f"/admin/production/{p.id}/outline/adopt", revision=str(d.projects.get(p.id).revision), candidate_id=str(candidate), matches_json='{"1":null,"2":null}')
    assert adoption.status_code == 303 and len(d.drafts.freeze_input(p.id).chapters) == 2


def test_candidate_adopt_reject_and_cross_project_reference_guards(production_data):
    from course_platform.production.models import ReferenceInput
    from tests.production.test_drafts import generate
    d, c = production_data, production_data.client
    base = f"/admin/production/{d.project_id}"
    first = generate(d.db_path, d.clock, d.actor, d.drafts, d.project_id, d.chapter_id, None, "<script>候选源码</script>")
    page = c.get(base + f"/candidates/{first.candidate_id}")
    assert page.status_code == 200 and "<script>候选源码</script>" not in page.text
    response = post(c, base + f"/candidates/{first.candidate_id}/adopt", revision=str(d.projects.get(d.project_id).revision), body_revision="")
    assert response.status_code == 303
    assert d.drafts.freeze_input(d.project_id).chapters[0].markdown == "<script>候选源码</script>"
    second = generate(d.db_path, d.clock, d.actor, d.drafts, d.project_id, d.chapter_id, 2, "拒绝候选")
    assert post(c, base + f"/candidates/{second.candidate_id}/reject", revision=str(d.projects.get(d.project_id).revision)).status_code == 303
    other = d.projects.create(d.actor, BriefInput(title="另一项目"), mode="quick")
    reference = d.projects.save_reference(d.actor, other.id, other.revision, None, ReferenceInput(text="外部项目参考"))
    for path in (base + f"/references/{reference.id}/remove", base + "/references/save"):
        response = post(c, path, revision=str(d.projects.get(d.project_id).revision), reference_id=str(reference.id), text="不能回显本次资料")
        assert response.status_code == 404 and "不能回显本次资料" not in response.text


def test_gets_leave_job_ledger_and_sources_unchanged_then_post_maintains(production_data):
    from course_platform.production.prompts import build_chapter_request
    d, c = production_data, production_data.client
    queued = post(c, f"/admin/production/{d.project_id}/generate", revision="2", kind="chapters_generate", idempotency_key="lost-web")
    assert queued.status_code == 303
    state = c.app.state
    claim = state.recovery_job_service.repository.claim_next("lost-worker")
    with transaction(d.db_path, immediate=True) as conn:
        call_id = state.token_budget.begin_call_in_tx(conn, claim, build_chapter_request(claim.input, d.chapter_id, claim.config))
    d.clock.advance(seconds=120)
    def history():
        with transaction(d.db_path) as conn:
            return {table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1")] for table in (
                "production_projects", "outline_revisions", "chapter_revisions", "production_jobs", "job_attempts", "job_steps", "model_calls", "token_reservations", "admin_events")}
    before = history()
    for path in ("/admin/production", f"/admin/production/{d.project_id}", f"/admin/production/{d.project_id}/preview", "/admin/jobs", queued.headers["location"], "/admin/jobs/model"):
        assert c.get(path).status_code == 200
    assert history() == before
    assert post(c, "/admin/jobs/check-interrupted").status_code == 303
    with transaction(d.db_path) as conn:
        assert conn.execute("SELECT state FROM production_jobs WHERE id=?", (claim.job_id,)).fetchone()[0] == "interrupted"
        assert conn.execute("SELECT status FROM model_calls WHERE id=?", (call_id,)).fetchone()[0] == "unknown"
        assert conn.execute("SELECT released_tokens FROM token_reservations WHERE call_id=?", (call_id,)).fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["chapter", "submit", "maintenance", "resume"])
def test_production_service_work_does_not_block_unrelated_requests(production_data, monkeypatch, operation):
    from threading import Event, Thread
    import httpx
    d, c = production_data, production_data.client
    state = c.app.state
    headers = {"Origin": "http://testserver"}
    data = {"csrf_token": csrf(c), "revision": "2", "body_revision": "", "markdown": "offloaded-source",
        "kind": "chapters_generate", "idempotency_key": "offload-job", "confirm": "yes"}
    if operation == "chapter":
        path, target, name = f"/admin/production/{d.project_id}/chapters/{d.chapter_id}/save", state.draft_service, "save_chapter"
    elif operation == "submit":
        path, target, name = f"/admin/production/{d.project_id}/generate", state.job_service, "submit"
    elif operation == "maintenance":
        import course_platform.jobs.worker as target
        path, name = "/admin/jobs/check-interrupted", "maintain"
    else:
        queued = post(c, f"/admin/production/{d.project_id}/generate", revision="2", kind="chapters_generate", idempotency_key="offload-parent")
        assert queued.status_code == 303
        assert post(c, queued.headers["location"] + "/cancel", revision="1").status_code == 303
        path, target, name = queued.headers["location"] + "/resume", state.recovery_job_service, "resume"
    real = getattr(target, name)
    entered, release, progressed = Event(), Event(), Event()
    def gated(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real(*args, **kwargs)
    monkeypatch.setattr(target, name, gated)
    @c.app.get("/task9-unrelated")
    async def unrelated():
        return {"available": True}
    def watchdog():
        entered.wait(5)
        progressed.wait(1)
        release.set()
    watcher = Thread(target=watchdog)
    watcher.start()
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=c.app), base_url="http://testserver", cookies=dict(c.cookies.items())) as client:
            pending = asyncio.create_task(client.post(path, data=data, headers=headers))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                probe = await client.get("/task9-unrelated")
                before_release = not release.is_set()
                progressed.set()
            finally:
                release.set()
            response = await pending
            assert probe.status_code == 200 and probe.json() == {"available": True}
            assert before_release, "synchronous production service blocked the ASGI loop"
            assert response.status_code == 303
    try:
        asyncio.run(exercise())
    finally:
        release.set()
        watcher.join(timeout=5)
    if operation == "chapter":
        assert d.drafts.freeze_input(d.project_id).chapters[0].markdown == "offloaded-source"


def maintenance_markers(data):
    with transaction(data.db_path) as conn:
        return [dict(r) for r in conn.execute("""SELECT * FROM admin_events WHERE action='job.interrupt'
            AND object_type='job' AND object_id='maintenance' AND reason='maintenance_checked'
            AND outcome='success' ORDER BY id""")]


def prepare_lost_call(data):
    from course_platform.production.prompts import build_chapter_request
    queued = post(data.client, f"/admin/production/{data.project_id}/generate", revision=str(data.projects.get(data.project_id).revision),
        kind="chapters_generate", idempotency_key="marker-lost-call")
    assert queued.status_code == 303
    state = data.client.app.state
    claim = state.recovery_job_service.repository.claim_next("marker-lost-worker")
    with transaction(data.db_path, immediate=True) as conn:
        call_id = state.token_budget.begin_call_in_tx(conn, claim, build_chapter_request(claim.input, data.chapter_id, claim.config))
    data.clock.advance(seconds=120)
    return claim, call_id


def test_explicit_maintenance_zero_repeat_and_latest_id_readonly(production_data, monkeypatch):
    from course_platform.audit import AuditEvent, append_event
    d, c = production_data, production_data.client
    monkeypatch.setattr("course_platform.audit.utc_now", d.clock.now)
    initial = c.get("/admin/jobs")
    assert "最近明确检查时间：尚未记录" in initial.text
    assert post(c, "/admin/jobs/check-interrupted", action="auth.login", object_id="forged", reason="forged",
        markdown="marker-private-source", api_key="marker-private-key").status_code == 303
    markers = maintenance_markers(d)
    assert len(markers) == 1
    assert markers[0]["actor_admin_id"] == d.actor.admin_id and json.loads(markers[0]["changes_json"]) == {}
    assert re.fullmatch(r"[0-9a-f]{32}", markers[0]["request_id"])
    d.clock.advance(seconds=1)
    assert post(c, "/admin/jobs/check-interrupted").status_code == 303
    markers = maintenance_markers(d)
    assert len(markers) == 2 and markers[1]["id"] > markers[0]["id"]
    # Higher-ID interrupted-job and denied-check records must not masquerade
    # as a successful explicit maintenance check.
    with transaction(d.db_path, immediate=True) as conn:
        append_event(conn, AuditEvent(d.actor.admin_id, "job", "123", "job.interrupt", "completed", "success", "other-job", {}))
        append_event(conn, AuditEvent(d.actor.admin_id, "job", "maintenance", "job.interrupt", "maintenance_checked", "denied", "denied-check", {}))
        before = [dict(r) for r in conn.execute("SELECT * FROM admin_events ORDER BY id")]
    page = c.get("/admin/jobs")
    assert page.status_code == 200
    assert 'datetime="' + markers[-1]["created_at"] + '">2026-10-02 08:00:01 Asia/Shanghai</time>' in page.text
    assert "并不代表 worker 周期维护时间或发生了任务中断" in page.text
    with transaction(d.db_path) as conn:
        assert [dict(r) for r in conn.execute("SELECT * FROM admin_events ORDER BY id")] == before
    assert "marker-private-source" not in str(markers) and "marker-private-key" not in str(markers)


def test_successful_explicit_maintenance_nonzero_marker_keeps_unknown(production_data, monkeypatch):
    d, c = production_data, production_data.client
    claim, call_id = prepare_lost_call(d)
    assert post(c, "/admin/jobs/check-interrupted").status_code == 303
    markers = maintenance_markers(d)
    assert len(markers) == 1
    with transaction(d.db_path) as conn:
        assert conn.execute("SELECT state FROM production_jobs WHERE id=?", (claim.job_id,)).fetchone()[0] == "interrupted"
        assert conn.execute("SELECT status FROM model_calls WHERE id=?", (call_id,)).fetchone()[0] == "unknown"
        assert conn.execute("SELECT released_tokens FROM token_reservations WHERE call_id=?", (call_id,)).fetchone()[0] == 0
        events = [dict(r) for r in conn.execute("SELECT * FROM admin_events WHERE action='job.interrupt' AND outcome='success' ORDER BY id")]
        assert any(r["object_id"] == str(claim.job_id) for r in events)
        assert events[-1]["object_id"] == "maintenance"


@pytest.mark.parametrize("fault,status", [("anonymous", 401), ("csrf", 403), ("origin", 403), ("failed", 503)])
def test_failed_or_unauthorized_explicit_maintenance_has_no_marker(production_data, monkeypatch, fault, status):
    from course_platform.jobs import worker
    from course_platform.domain import BusinessError
    d, c = production_data, production_data.client
    before = maintenance_markers(d)
    def failure(*args, **kwargs):
        if fault == "failed":
            raise BusinessError("maintenance_unavailable", "明确检查暂不可用。", 503)
        raise AssertionError("unauthorized maintenance must not run")
    monkeypatch.setattr(worker, "maintain", failure)
    data = {"csrf_token": csrf(c), "markdown": "private-failed-maintain-copy"}
    if fault == "anonymous":
        c.cookies.clear()
    elif fault == "csrf":
        data["csrf_token"] = "b" * 43
    response = c.post("/admin/jobs/check-interrupted", data=data,
        headers={"Origin": "http://evil.test" if fault == "origin" else "http://testserver"})
    assert response.status_code == status and response.headers["cache-control"] == "no-store"
    assert "private-failed-maintain-copy" not in response.text
    assert maintenance_markers(d) == before


def test_explicit_maintenance_marker_failure_preserves_repair_and_old_time(production_data, monkeypatch, caplog):
    import logging
    import course_platform.admin.routes.jobs as routes
    d, c = production_data, production_data.client
    assert post(c, "/admin/jobs/check-interrupted").status_code == 303
    old = maintenance_markers(d)
    assert len(old) == 1
    claim, call_id = prepare_lost_call(d)
    real_append = routes.append_event
    def failed_marker(conn, event):
        real_append(conn, event)
        raise RuntimeError("private-raw-marker-error")
    monkeypatch.setattr(routes, "append_event", failed_marker)
    caplog.set_level(logging.DEBUG)
    response = post(c, "/admin/jobs/check-interrupted")
    assert response.status_code == 500 and response.headers["cache-control"] == "no-store"
    assert maintenance_markers(d) == old
    with transaction(d.db_path) as conn:
        assert conn.execute("SELECT state FROM production_jobs WHERE id=?", (claim.job_id,)).fetchone()[0] == "interrupted"
        assert conn.execute("SELECT status FROM model_calls WHERE id=?", (call_id,)).fetchone()[0] == "unknown"
        assert conn.execute("SELECT released_tokens FROM token_reservations WHERE call_id=?", (call_id,)).fetchone()[0] == 0
    from datetime import datetime, timedelta
    old_time = old[-1]["created_at"]
    expected_time = (datetime.fromisoformat(old_time) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    assert 'datetime="' + old_time + '">' + expected_time + ' Asia/Shanghai</time>' in c.get("/admin/jobs").text
    assert "private-raw-marker-error" not in response.text + caplog.text


def test_explicit_maintenance_marker_rechecks_current_enabled_owner(production_data, monkeypatch):
    from course_platform.jobs import worker
    d, c = production_data, production_data.client
    real = worker.maintain
    def disable_after_coordinator(*args, **kwargs):
        result = real(*args, **kwargs)
        with transaction(d.db_path, immediate=True) as conn:
            conn.execute("UPDATE admins SET enabled=0")
        return result
    monkeypatch.setattr(worker, "maintain", disable_after_coordinator)
    response = post(c, "/admin/jobs/check-interrupted")
    assert response.status_code == 403
    assert not maintenance_markers(d)
