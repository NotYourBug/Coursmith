"""Durable worker behavior using real SQLite and controlled model responses."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from course_platform.database import transaction
from course_platform.production.llm import ModelFailure, ModelResult, Usage
from course_platform.production.models import BriefInput, OutlineInput, OutlineItem
from tests.jobs.fakes import ScriptedModel


BODY = "## 学习目标\n了解。\n## 讲解\n解释。\n## 案例\n例子。\n## 练习\n实践。\n## 总结\n回顾。"


def result(text=BODY):
    return ModelResult(text, Usage(12, 4), "cmpl-controlled")


def outline(count):
    return result(json.dumps({"items": [{"title": f"第{i}章", "goal": "学习"}
                                          for i in range(1, count + 1)]}, ensure_ascii=False))


def setup_run(jobs_context, *, count=2, mode="quick", kind="course_draft_generate",
              existing=False, bodies=False, confirmed=False, model=None):
    c = jobs_context()
    p = None
    if kind != "connection_test":
        p = c.projects.create(c.actor, BriefInput(title="课程", chapter_count=count), mode=mode)
        if existing:
            o = c.drafts.save_outline(c.actor, p.id, p.revision, OutlineInput(items=[
                OutlineItem(chapter_id=None, title=f"第{i}章", goal="学习")
                for i in range(1, count + 1)]))
            if bodies:
                for item in o.items:
                    c.drafts.save_chapter(c.actor, p.id, item.chapter_id, None, "人工旧正文")
            if confirmed:
                c.drafts.confirm_outline(c.actor, p.id, c.projects.get(p.id).revision)
            p = c.projects.get(p.id)
    selected = (c.drafts.freeze_input(p.id).chapters[0].id,) if kind == "chapter_regenerate" else ()
    job = c.submit(p, kind=kind, chapter_ids=selected)
    scripted = model or ScriptedModel(([outline(count)] if kind in (
        "course_draft_generate", "outline_generate") else []) + [result()] * count)
    return SimpleNamespace(c=c, p=p, job=job, model=scripted, selected=selected)


def worker(run, **changes):
    from course_platform.jobs.worker import Worker

    return Worker(run.c.repository, run.c.drafts, run.c.budget, run.model,
                  changes.get("config", run.c.execution_config), worker_id="test-worker", clock=run.c.clock.now)


@pytest.fixture
def quick_run(jobs_context):
    return setup_run(jobs_context, count=10)


def test_quick_title_only_creates_outline_and_draft_without_confirmation(quick_run, read_rows):
    r = quick_run
    assert asyncio.run(worker(r).run_once()) is True
    frozen = r.c.drafts.freeze_input(r.p.id)
    assert len(frozen.chapters) == 10
    assert all(c.markdown == BODY for c in frozen.chapters)
    assert frozen.outline.confirmed is False
    assert r.c.service.get(r.job.id).state == "succeeded"
    assert len(r.model.calls) == 11
    assert not read_rows("SELECT * FROM outline_confirmations")
    assert len(read_rows("SELECT * FROM job_steps WHERE state='succeeded'")) == 12


def test_precise_outline_stops_before_chapter_calls(jobs_context, read_rows):
    r = setup_run(jobs_context, mode="precise", kind="outline_generate")
    asyncio.run(worker(r).run_once())
    assert len(r.model.calls) == 1
    assert r.c.service.get(r.job.id).state == "succeeded"
    assert r.c.drafts.freeze_input(r.p.id).outline is None
    assert len(read_rows("SELECT * FROM production_candidates WHERE kind='outline'")) == 1
    assert not read_rows("SELECT * FROM chapter_revisions")


def test_first_blank_body_adopts_but_existing_body_only_candidate(jobs_context, read_rows):
    r = setup_run(jobs_context, existing=True, kind="chapters_generate")
    chapter = r.c.drafts.freeze_input(r.p.id).chapters[1]
    # Existing chapter is editable while queued; submission must freeze that body.
    with transaction(r.c.db_path, immediate=True) as conn:
        conn.execute("UPDATE production_jobs SET state='cancelled' WHERE id=?", (r.job.id,))
    r.c.drafts.save_chapter(r.c.actor, r.p.id, chapter.id, None, "人工旧正文")
    r.job = r.c.submit(r.c.projects.get(r.p.id), kind="chapters_generate", key="after-body")
    asyncio.run(worker(r).run_once())
    frozen = r.c.drafts.freeze_input(r.p.id)
    assert frozen.chapters[0].markdown == BODY
    assert frozen.chapters[1].markdown == "人工旧正文"
    candidate = read_rows("SELECT * FROM production_candidates WHERE kind='chapter'")[0]
    step = read_rows("SELECT * FROM job_steps WHERE candidate_id=?", (candidate["id"],))[0]
    assert step["state"] == "succeeded"
    assert step["chapter_revision_id"] == candidate["chapter_revision_id"]
    assert step["input_hash"] == candidate["input_hash"]
    r.c.drafts.adopt_candidate(r.c.actor, r.p.id, r.c.projects.get(r.p.id).revision,
                             candidate["id"], frozen.chapters[1].body_revision)
    assert r.c.drafts.freeze_input(r.p.id).chapters[1].markdown == BODY


def test_partial_failure_keeps_completed_chapters_and_job_failed(jobs_context, read_rows):
    r = setup_run(jobs_context, model=ScriptedModel([outline(2), result(),
                                                  ModelFailure("response_unknown", None)]))
    asyncio.run(worker(r).run_once())
    frozen = r.c.drafts.freeze_input(r.p.id)
    assert frozen.chapters[0].markdown == BODY
    assert frozen.chapters[1].body_revision is None
    assert r.c.service.get(r.job.id).state == "failed"
    assert [s["state"] for s in read_rows("SELECT * FROM job_steps ORDER BY step_number")] == [
        "succeeded", "succeeded", "failed"]
    unknown = read_rows("SELECT * FROM token_reservations WHERE state='unknown'")[0]
    assert unknown["released_tokens"] == 0


def test_long_call_heartbeats_at_10_seconds(jobs_context, monkeypatch, read_rows):
    from course_platform.jobs import worker as module

    async def run():
        gate, tick = asyncio.Event(), asyncio.Event()
        r = setup_run(jobs_context, kind="connection_test", model=ScriptedModel([result("OK")], barrier=gate))
        async def controlled_sleep(seconds):
            assert seconds == 10
            await tick.wait()
            tick.clear()
            r.c.clock.advance(seconds=10)
        monkeypatch.setattr(module, "sleep", controlled_sleep)
        task = asyncio.create_task(worker(r).run_once())
        await r.model.sent.wait()
        with transaction(r.c.db_path, immediate=True) as conn:
            conn.execute("UPDATE production_jobs SET reason_code='network_unlocked' WHERE id=?", (r.job.id,))
        for expected in (10, 20, 30):
            tick.set()
            for _ in range(1000):
                await asyncio.sleep(0.001)
                stamp = read_rows("SELECT heartbeat_at FROM job_attempts")[0]["heartbeat_at"]
                if stamp == r.c.clock.now().isoformat() and r.c.clock.now().second == expected:
                    break
            else:
                pytest.fail("independent heartbeat was blocked by the network call")
        gate.set()
        assert await task is True
        assert r.c.service.get(r.job.id).state == "succeeded"
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    asyncio.run(run())


def test_crash_after_send_marks_unknown_without_replay(jobs_context, read_rows):
    from course_platform.jobs.worker import maintain
    from course_platform.production.prompts import build_connection_request

    r = setup_run(jobs_context, kind="connection_test", model=ScriptedModel([result("OK")]))
    claim = r.c.repository.claim_next("crashed-worker")
    with transaction(r.c.db_path, immediate=True) as conn:
        call_id = r.c.budget.begin_call_in_tx(conn, claim, build_connection_request(claim.config))
    r.c.clock.advance(seconds=60)
    assert maintain(r.c.repository, r.c.budget) == 0
    assert read_rows("SELECT status FROM model_calls WHERE id=?", (call_id,))[0]["status"] == "sent"
    r.c.clock.advance(seconds=60)
    assert maintain(r.c.repository, r.c.budget) == 1
    assert asyncio.run(worker(r).run_once()) is False
    assert not r.model.calls
    assert r.c.service.get(r.job.id).state == "interrupted"
    call = read_rows("SELECT * FROM model_calls WHERE id=?", (call_id,))[0]
    reserve = read_rows("SELECT * FROM token_reservations WHERE call_id=?", (call_id,))[0]
    assert call["status"] == "unknown" and reserve["released_tokens"] == 0
    assert reserve["state"] == "unknown"
    pool = read_rows("SELECT * FROM token_reservations WHERE job_id=? AND call_id IS NULL", (r.job.id,))[0]
    history = read_rows("SELECT * FROM admin_events ORDER BY id")
    assert pool["settled_at"] is not None
    assert pool["released_tokens"] == pool["reserved_tokens"]
    for elapsed in (10, 20):
        r.c.clock.advance(seconds=elapsed)
        assert maintain(r.c.repository, r.c.budget) == 0
        repeated_pool = read_rows("SELECT * FROM token_reservations WHERE job_id=? AND call_id IS NULL", (r.job.id,))[0]
        assert repeated_pool["settled_at"] == pool["settled_at"]
        assert repeated_pool == pool
        assert read_rows("SELECT * FROM model_calls WHERE id=?", (call_id,))[0] == call
        assert read_rows("SELECT * FROM token_reservations WHERE call_id=?", (call_id,))[0] == reserve
        assert read_rows("SELECT * FROM admin_events ORDER BY id") == history


def test_late_response_after_lease_loss_only_isolated_history(jobs_context, read_rows):
    async def run():
        gate = asyncio.Event()
        r = setup_run(jobs_context, existing=True, kind="chapters_generate",
                      model=ScriptedModel([result()], barrier=gate))
        before = r.c.drafts.freeze_input(r.p.id)
        task = asyncio.create_task(worker(r).run_once())
        await r.model.sent.wait()
        r.c.clock.advance(seconds=60)
        gate.set()
        await task
        assert r.c.drafts.freeze_input(r.p.id).input_hash == before.input_hash
        assert not read_rows("SELECT * FROM production_candidates")
        assert not read_rows("SELECT * FROM chapter_revisions")
        call = read_rows("SELECT * FROM model_calls")[0]
        assert call["status"] == "unknown"
        assert call["response_disposition"] == "isolated" and call["response_text"] == BODY
        assert call["input_tokens"] == 12
        assert not read_rows("SELECT * FROM job_steps WHERE state='succeeded'")
    asyncio.run(run())


def test_cancelled_derived_candidate_keeps_proof_for_later_human_adoption(jobs_context, read_rows):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        class Model(ScriptedModel):
            async def generate(self, request, *, deadline_seconds=120):
                value = await super().generate(request, deadline_seconds=deadline_seconds)
                if len(self.calls) == 3:
                    entered.set()
                    await release.wait()
                return value
        r = setup_run(jobs_context, model=Model([outline(2), result(), result()]))
        task = asyncio.create_task(worker(r).run_once())
        await entered.wait()
        frozen = r.c.drafts.freeze_input(r.p.id)
        r.c.drafts.save_chapter(r.c.actor, r.p.id, frozen.chapters[0].id,
                               frozen.chapters[0].body_revision, "人工修改已完成章")
        with transaction(r.c.db_path, immediate=True) as conn:
            conn.execute("UPDATE production_jobs SET cancel_requested=1 WHERE id=?", (r.job.id,))
        release.set()
        await task
        assert r.c.service.get(r.job.id).state == "cancelled"
        assert len(r.model.calls) == 3
        candidate = read_rows("SELECT * FROM production_candidates WHERE kind='chapter'")[0]
        step = read_rows("SELECT * FROM job_steps WHERE candidate_id=?", (candidate["id"],))[0]
        assert step["state"] == "succeeded"
        r.c.drafts.adopt_candidate(r.c.actor, r.p.id, r.c.projects.get(r.p.id).revision,
                                 candidate["id"], None)
        current = r.c.drafts.freeze_input(r.p.id)
        assert current.chapters[0].markdown == "人工修改已完成章"
        assert current.chapters[1].markdown == BODY
    asyncio.run(run())


@pytest.mark.parametrize("drift", ["config", "owner", "cancel", "source"])
def test_each_new_send_rechecks_execution_authority(jobs_context, read_rows, drift):
    r = setup_run(jobs_context, existing=True, kind="chapters_generate")
    class Model(ScriptedModel):
        async def generate(self, request, *, deadline_seconds=120):
            value = await super().generate(request, deadline_seconds=deadline_seconds)
            return value
    r.model = Model([result(), result()])
    from course_platform.jobs import handlers
    original = handlers.Handlers._complete_step
    def after_step(*args, **kwargs):
        value = original(*args, **kwargs)
        conn = args[0]
        if drift == "owner":
            conn.execute("UPDATE admins SET enabled=0 WHERE id=?", (r.c.actor.admin_id,))
        elif drift == "cancel":
            conn.execute("UPDATE production_jobs SET cancel_requested=1 WHERE id=?", (r.job.id,))
        elif drift == "source":
            row = conn.execute("SELECT * FROM production_chapters WHERE project_id=? AND current_revision_id IS NOT NULL", (r.p.id,)).fetchone()
            conn.execute("UPDATE production_chapters SET current_revision_id=NULL WHERE id=?", (row["id"],))
        return value
    if drift == "config":
        config = replace(r.c.execution_config, public=r.c.config.model_copy(update={"prompt_version": "v2"}))
        asyncio.run(worker(r, config=config).run_once())
        assert not r.model.calls
    else:
        # Concrete persisted mutations at a transaction boundary, not fence mocks.
        from unittest.mock import patch
        with patch.object(handlers.Handlers, "_complete_step", side_effect=after_step):
            asyncio.run(worker(r).run_once())
        assert len(r.model.calls) == 1


def test_worker_idle_and_stopped_loop_do_not_call_model(jobs_context):
    from course_platform.jobs.worker import Worker
    c = jobs_context()
    model = ScriptedModel([])
    w = Worker(c.repository, c.drafts, c.budget, model, c.execution_config,
               worker_id="idle-worker", clock=c.clock.now)
    assert asyncio.run(w.run_once()) is False
    async def stopped():
        stop = asyncio.Event()
        stop.set()
        await w.run_forever(stop)
    asyncio.run(stopped())
    assert not model.calls
