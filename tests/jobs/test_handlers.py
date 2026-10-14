"""Handler plan, ledger, atomicity and provenance checks over real storage."""

import asyncio
from dataclasses import replace

import pytest

from course_platform.database import transaction
from course_platform.production.llm import ModelFailure
from tests.jobs.fakes import ScriptedModel
from tests.jobs.test_worker import BODY, outline, result, setup_run, worker


def test_safe_retries_have_distinct_call_rows_and_backoff(jobs_context, monkeypatch, read_rows):
    from course_platform.jobs import handlers
    r = setup_run(jobs_context, existing=True, count=1, kind="chapters_generate",
                  model=ScriptedModel([ModelFailure("not_sent", False), ModelFailure("not_sent", False), result()]))
    waits = []
    async def wait(seconds):
        rows = read_rows("SELECT * FROM model_calls ORDER BY id")
        assert rows[-1]["status"] == "rejected"
        reserves = read_rows("SELECT * FROM token_reservations WHERE call_id IS NOT NULL ORDER BY id")
        assert reserves[-1]["released_tokens"] == reserves[-1]["reserved_tokens"]
        waits.append(seconds)
    monkeypatch.setattr(handlers, "sleep", wait)
    asyncio.run(worker(r).run_once())
    assert waits == [2, 8]
    rows = read_rows("SELECT * FROM model_calls ORDER BY id")
    assert [row["status"] for row in rows] == ["rejected", "rejected", "successful"]
    assert [row["call_number"] for row in rows] == [1, 2, 3]
    assert len({row["id"] for row in rows}) == 3
    assert len({row["call_id"] for row in read_rows("SELECT * FROM token_reservations WHERE call_id IS NOT NULL")}) == 3
    assert r.c.service.get(r.job.id).state == "succeeded"


@pytest.mark.parametrize("failure", [ModelFailure("response_unknown", None), ModelFailure("authentication", False),
                                    ModelFailure("invalid_response", True)])
def test_non_proven_failure_retains_unknown_and_stops(jobs_context, read_rows, failure):
    r = setup_run(jobs_context, existing=True, kind="chapters_generate", model=ScriptedModel([failure, result()]))
    asyncio.run(worker(r).run_once())
    assert len(r.model.calls) == 1
    assert r.c.service.get(r.job.id).state == "failed"
    call = read_rows("SELECT * FROM model_calls")[0]
    reserve = read_rows("SELECT * FROM token_reservations WHERE call_id=?", (call["id"],))[0]
    assert call["status"] == "unknown"
    assert reserve["state"] == "unknown" and reserve["released_tokens"] == 0


def test_connection_test_is_once_32_tokens_even_for_proven_rejection(jobs_context, read_rows):
    r = setup_run(jobs_context, kind="connection_test", model=ScriptedModel([ModelFailure("not_sent", False), result("OK")]))
    asyncio.run(worker(r).run_once())
    assert len(r.model.calls) == 1 and r.model.calls[0].max_output_tokens == 32
    assert r.c.service.get(r.job.id).state == "failed"
    assert read_rows("SELECT status FROM model_calls")[0]["status"] == "rejected"


def test_draft_validate_is_local_and_records_bound_check(jobs_context, read_rows):
    r = setup_run(jobs_context, existing=True, bodies=True, kind="draft_validate", model=ScriptedModel([]))
    asyncio.run(worker(r).run_once())
    assert not r.model.calls
    assert r.c.service.get(r.job.id).state == "succeeded"
    step = read_rows("SELECT * FROM job_steps")[0]
    check = read_rows("SELECT * FROM draft_checks")[0]
    assert step["check_id"] == check["id"] and step["input_hash"] == check["input_hash"]
    assert check["blocking"] == 1 and check["human_review_required"] == 1


def test_existing_outline_redo_only_candidate_and_stops(jobs_context, read_rows):
    r = setup_run(jobs_context, existing=True)
    frozen = r.c.drafts.freeze_input(r.p.id)
    asyncio.run(worker(r).run_once())
    assert r.c.drafts.freeze_input(r.p.id).input_hash == frozen.input_hash
    assert len(r.model.calls) == 1
    assert r.c.service.get(r.job.id).state == "succeeded"
    assert len(read_rows("SELECT * FROM production_candidates WHERE kind='outline'")) == 1


def test_precise_batch_confirmation_rechecked_in_execution(jobs_context, read_rows):
    r = setup_run(jobs_context, existing=True, confirmed=True, mode="precise", kind="chapters_generate")
    with transaction(r.c.db_path, immediate=True) as conn:
        conn.execute("UPDATE production_projects SET current_confirmation_id=NULL WHERE id=?", (r.p.id,))
    asyncio.run(worker(r).run_once())
    assert not r.model.calls
    assert r.c.service.get(r.job.id).reason_code == "outline_unconfirmed"


def test_actual_remaining_plan_rechecked_before_first_chapter(jobs_context, monkeypatch, read_rows):
    r = setup_run(jobs_context)
    original = r.c.budget.estimate
    def expensive_actual(request):
        if request.messages[0]["content"].startswith("Write a private course chapter"):
            return 110000
        return original(request)
    monkeypatch.setattr(r.c.budget, "estimate", expensive_actual)
    asyncio.run(worker(r).run_once())
    assert len(r.model.calls) == 1
    assert r.c.service.get(r.job.id).reason_code == "token_budget_exceeded"
    assert r.c.drafts.freeze_input(r.p.id).outline is not None
    assert not read_rows("SELECT * FROM chapter_revisions")


def test_quick_never_sends_estimation_placeholders(jobs_context):
    r = setup_run(jobs_context)
    asyncio.run(worker(r).run_once())
    assert len(r.model.calls) == 3
    assert all("ESTIMATE ONLY" not in call.messages[0]["content"] for call in r.model.calls)
    assert all("x" * 1000 not in call.messages[-1]["content"] for call in r.model.calls)


def test_missing_planned_step_refuses_success(jobs_context, monkeypatch):
    from course_platform.jobs.handlers import Handlers
    r = setup_run(jobs_context, existing=True, kind="chapters_generate")
    original = Handlers._require_complete
    def hide(conn, claim, expected):
        # One planned step is absent; all persisted steps have already succeeded.
        # Schema correctly forbids deleting historical rows to construct this case.
        return original(conn, claim, [*expected, ("validate", None)])
    monkeypatch.setattr(Handlers, "_require_complete", staticmethod(hide))
    asyncio.run(worker(r).run_once())
    assert r.c.service.get(r.job.id).state == "failed"
    assert r.c.service.get(r.job.id).reason_code == "job_steps_incomplete"


def test_generation_settlement_pointer_step_and_audit_roll_back_together(jobs_context, read_rows):
    r = setup_run(jobs_context, existing=True, count=1, kind="chapters_generate")
    with transaction(r.c.db_path, immediate=True) as conn:
        conn.execute("""CREATE TRIGGER reject_generated_audit BEFORE INSERT ON admin_events
            WHEN NEW.action='production.chapter' BEGIN SELECT RAISE(ABORT,'controlled'); END""")
    asyncio.run(worker(r).run_once())
    assert not read_rows("SELECT * FROM chapter_revisions")
    assert not read_rows("SELECT * FROM job_steps WHERE state='succeeded'")
    call = read_rows("SELECT * FROM model_calls")[0]
    assert call["status"] == "successful" and call["input_tokens"] == 12
    assert call["response_disposition"] == "active"
    assert not read_rows("SELECT * FROM admin_events WHERE action IN ('job.step','production.chapter')")
    assert r.c.service.get(r.job.id).state == "failed"


def test_key_rotation_preserves_frozen_execution(jobs_context):
    r = setup_run(jobs_context, kind="connection_test", model=ScriptedModel([result("OK")]))
    asyncio.run(worker(r, config=replace(r.c.execution_config, api_key="rotated-fake-key")).run_once())
    assert r.c.service.get(r.job.id).state == "succeeded" and len(r.model.calls) == 1


def test_cancel_during_adapter_call_registration_retains_unknown_receipt(jobs_context, read_rows, monkeypatch):
    import threading
    import httpx
    from course_platform.jobs.handlers import Handlers
    from course_platform.production.llm import OpenAIModel
    from tests.jobs.fakes import ModelTransport

    entered, release = threading.Event(), threading.Event()
    r = setup_run(jobs_context, kind="connection_test")
    original = Handlers._begin
    def registering(self, request):
        call_id = original(self, request)
        entered.set()
        assert release.wait(3)
        return call_id
    monkeypatch.setattr(Handlers, "_begin", registering)

    async def exercise():
        transport = ModelTransport([])
        async with httpx.AsyncClient(transport=transport) as client:
            r.model = OpenAIModel(r.c.execution_config, http_client=client)
            task = asyncio.create_task(worker(r).run_once())
            try:
                assert await asyncio.to_thread(entered.wait, 45)
                task.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                call = read_rows("SELECT * FROM model_calls")[0]
                reserve = read_rows("SELECT * FROM token_reservations WHERE call_id=?", (call["id"],))[0]
                assert call["status"] == "unknown", "Registration receipt was lost at cancellation"
                assert reserve["state"] == "unknown" and reserve["released_tokens"] == 0
                assert transport.calls == []
                assert client.event_hooks["response"] == [] and not client.is_closed
            finally:
                release.set()
                if not task.done():
                    await task
    asyncio.run(exercise())


@pytest.mark.parametrize("cancellation", ["nested_deadlines", "repeated_external", "registration_failure"])
def test_adapter_registration_outcome_survives_repeated_cancellation(
    jobs_context, read_rows, monkeypatch, caplog, cancellation,
):
    import logging
    import threading

    import httpx
    from course_platform.jobs import handlers
    from course_platform.production import llm

    __import__("openai")  # Preload the installed SDK before the registration gate.
    r = setup_run(jobs_context, existing=True, bodies=True, kind="chapter_regenerate")
    frozen = r.c.drafts.freeze_input(r.p.id)
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    original_begin = handlers.Handlers._begin
    original_timeout_at = asyncio.timeout_at
    deadlines, http_calls = [], []
    executions = []

    def registering(execution, request):
        executions.append(execution)
        call_id = None if cancellation == "registration_failure" else original_begin(execution, request)
        entered.set()
        try:
            assert release.wait(3), "Bounded receipt gate was not released"
            if cancellation == "registration_failure":
                raise ValueError("private-registration-failure/fake-private-key")
            return call_id
        finally:
            returned.set()

    def deadline(_seconds):
        context = original_timeout_at(None)
        deadlines.append(context)
        return context

    async def forbidden_http(request):
        http_calls.append(request)
        raise AssertionError("Cancelled registration started HTTP")

    monkeypatch.setattr(handlers.Handlers, "_begin", registering)
    if cancellation == "nested_deadlines":
        monkeypatch.setattr(handlers, "timeout", deadline)
        monkeypatch.setattr(asyncio, "timeout_at", deadline)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden_http)) as client:
            r.model = llm.OpenAIModel(r.c.execution_config, http_client=client)
            task = asyncio.create_task(worker(r).run_once())
            try:
                assert await asyncio.to_thread(entered.wait, 3), "Registration was never reached"
                if cancellation == "nested_deadlines":
                    assert len(deadlines) == 2
                    loop = asyncio.get_running_loop()
                    deadlines[0].reschedule(loop.time() + .02)
                    deadlines[1].reschedule(loop.time() + .04)
                    async with original_timeout_at(loop.time() + 1):
                        while not all(context.expired() for context in deadlines):
                            await asyncio.sleep(.005)
                    assert all(context.expired() for context in deadlines)
                else:
                    task.cancel("original-worker-stop")
                    await asyncio.sleep(0)  # Deliver the first signal while the receipt stays held.
                    if cancellation == "repeated_external":
                        task.cancel("second-worker-stop")
                        await asyncio.sleep(0)
                release.set()
                if cancellation == "nested_deadlines":
                    assert await task is True
                    job = r.c.service.get(r.job.id)
                    assert (job.state, job.reason_code) == ("failed", "response_unknown")
                else:
                    with pytest.raises(asyncio.CancelledError) as cancelled:
                        await task
                    assert cancelled.value.args == ("original-worker-stop",)
                    # External task cancellation isolates the original attempt;
                    # unchanged explicit maintenance owns later interruption.
                    assert r.c.service.get(r.job.id).state == "running"
                assert await asyncio.to_thread(returned.wait, 1)
                await asyncio.sleep(0)
                calls = read_rows("SELECT * FROM model_calls")
                reserves = read_rows("SELECT * FROM token_reservations WHERE call_id IS NOT NULL")
                if cancellation == "registration_failure":
                    assert calls == [] and reserves == []
                    assert executions[0].call_id is None
                else:
                    assert len(calls) == len(reserves) == 1
                    call, reserve = calls[0], reserves[0]
                    assert executions[0].call_id == call["id"] == reserve["call_id"]
                    assert call["status"] == reserve["state"] == "unknown"
                    assert call["response_disposition"] == (
                        "active" if cancellation == "nested_deadlines" else "isolated")
                    assert call["error_code"] == (
                        "response_unknown" if cancellation == "nested_deadlines" else "worker_stopped")
                    assert call["response_text"] is None and call["response_hash"] is None
                    assert call["input_tokens"] is None and call["output_tokens"] is None
                    assert reserve["used_tokens"] is None and reserve["released_tokens"] == 0
                assert http_calls == []
                assert client.event_hooks["response"] == [] and not client.is_closed
                assert llm._PRIVATE_CALL.get() is False
                assert r.c.drafts.freeze_input(r.p.id) == frozen
                assert not read_rows("SELECT * FROM production_candidates")
                assert not [pending for pending in asyncio.all_tasks()
                            if pending is not asyncio.current_task() and not pending.done()]
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    with caplog.at_level(logging.DEBUG):
        asyncio.run(exercise())
    assert "private-registration-failure" not in caplog.text
    assert "fake-private-key" not in caplog.text


def test_unknown_usage_response_stops_spending_and_retains_private_history(jobs_context, read_rows):
    from course_platform.production.llm import ModelResult
    r = setup_run(jobs_context, existing=True, kind="chapters_generate",
                  model=ScriptedModel([ModelResult(BODY, None, "cmpl-known-response"), result()]))
    asyncio.run(worker(r).run_once())
    assert len(r.model.calls) == 1 and r.c.service.get(r.job.id).state == "failed"
    call = read_rows("SELECT * FROM model_calls")[0]
    assert call["status"] == "unknown" and call["response_text"] == BODY
    assert r.c.drafts.freeze_input(r.p.id).chapters[0].body_revision is None


def test_regeneration_existing_body_is_candidate(jobs_context, read_rows):
    r = setup_run(jobs_context, existing=True, bodies=True, kind="chapter_regenerate")
    asyncio.run(worker(r).run_once())
    assert len(r.model.calls) == 1
    assert all(c.markdown == "人工旧正文" for c in r.c.drafts.freeze_input(r.p.id).chapters)
    assert len(read_rows("SELECT * FROM production_candidates WHERE kind='chapter'")) == 1


def test_retry_revalidates_cancel_before_fresh_reservation(jobs_context, monkeypatch, read_rows):
    from course_platform.jobs import handlers
    r = setup_run(jobs_context, existing=True, kind="chapters_generate",
                  model=ScriptedModel([ModelFailure("not_sent", False), result()]))
    async def cancel(seconds):
        with transaction(r.c.db_path, immediate=True) as conn:
            conn.execute("UPDATE production_jobs SET cancel_requested=1 WHERE id=?", (r.job.id,))
    monkeypatch.setattr(handlers, "sleep", cancel)
    asyncio.run(worker(r).run_once())
    assert len(r.model.calls) == 1
    assert r.c.service.get(r.job.id).state == "cancelled"
    assert len(read_rows("SELECT * FROM model_calls")) == 1


def test_injected_model_obeys_total_deadline_without_real_wait(jobs_context, monkeypatch, read_rows):
    from course_platform.jobs import handlers
    original = asyncio.timeout
    monkeypatch.setattr(handlers, "timeout", lambda _: original(0))
    async def run():
        r = setup_run(jobs_context, kind="connection_test",
                      model=ScriptedModel([result()], barrier=asyncio.Event()))
        await worker(r).run_once()
        assert r.c.service.get(r.job.id).state == "failed"
        assert read_rows("SELECT status FROM model_calls")[0]["status"] == "unknown"
    asyncio.run(run())


def test_local_validation_works_without_key_or_context(jobs_context, read_rows):
    from course_platform.jobs.models import JobRequest
    from course_platform.jobs.worker import Worker
    c = jobs_context()
    config = replace(c.execution_config, api_key="", public=c.config.model_copy(update={"context_tokens": None}))
    c.service.configure_execution(config, c.budget)
    p = c.project(outline=True)
    job = c.service.submit(c.actor, JobRequest(kind="draft_validate", project_id=p.id, project_revision=p.revision),
                           config.public, idempotency_key="local-check", reserved_tokens=0)
    model = ScriptedModel([])
    w = Worker(c.repository, c.drafts, c.budget, model, config, worker_id="local-worker", clock=c.clock.now)
    asyncio.run(w.run_once())
    assert c.service.get(job.id).state == "succeeded"
    assert not model.calls and not read_rows("SELECT * FROM model_calls")
    assert read_rows("SELECT * FROM draft_checks")[0]["human_review_required"] == 1


def test_own_outputs_preserve_original_freeze_and_exact_step_call_sources(jobs_context, read_rows):
    r = setup_run(jobs_context)
    original = read_rows("SELECT input_json,input_hash FROM production_jobs WHERE id=?", (r.job.id,))[0]
    asyncio.run(worker(r).run_once())
    assert read_rows("SELECT input_json,input_hash FROM production_jobs WHERE id=?", (r.job.id,))[0] == original
    steps = read_rows("SELECT * FROM job_steps ORDER BY step_number")
    calls = read_rows("SELECT * FROM model_calls ORDER BY call_number")
    assert len({step["input_hash"] for step in steps}) == 4
    assert steps[0]["input_hash"] == original["input_hash"]
    for step, call in zip(steps[:3], calls):
        table = "outline_revisions" if step["kind"] == "outline" else "chapter_revisions"
        pointer = step["outline_revision_id"] if step["kind"] == "outline" else step["chapter_revision_id"]
        source = read_rows(f"SELECT * FROM {table} WHERE id=?", (pointer,))[0]
        assert call["step_id"] == step["id"]
        assert call["input_hash"] == step["input_hash"] == source["input_hash"]
        assert call["attempt_id"] == step["attempt_id"] == source["attempt_id"]
        assert call["job_id"] == step["job_id"] == source["job_id"] == r.job.id
        assert step["project_id"] == source["project_id"] == r.p.id


def test_manual_edit_during_inflight_preserves_candidate_then_stops_next_send(jobs_context, read_rows):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        class Model(ScriptedModel):
            async def generate(self, request, *, deadline_seconds=120):
                value = await super().generate(request, deadline_seconds=deadline_seconds)
                if len(self.calls) == 3:
                    entered.set()
                    await release.wait()
                return value
        r = setup_run(jobs_context, count=3, model=Model([outline(3), result(), result(), result()]))
        task = asyncio.create_task(worker(r).run_once())
        await entered.wait()
        current = r.c.drafts.freeze_input(r.p.id)
        r.c.drafts.save_chapter(r.c.actor, r.p.id, current.chapters[0].id,
                               current.chapters[0].body_revision, "人工内容")
        release.set()
        await task
        assert len(r.model.calls) == 3
        assert r.c.service.get(r.job.id).reason_code == "input_changed"
        candidate = read_rows("SELECT * FROM production_candidates WHERE kind='chapter'")[0]
        r.c.drafts.adopt_candidate(r.c.actor, r.p.id, r.c.projects.get(r.p.id).revision,
                                 candidate["id"], None)
        current = r.c.drafts.freeze_input(r.p.id)
        assert current.chapters[0].markdown == "人工内容"
        assert current.chapters[1].markdown == BODY and current.chapters[2].body_revision is None
    asyncio.run(run())


def test_coordinator_repairs_already_interrupted_sent_call_without_replay(jobs_context, read_rows):
    from course_platform.jobs.worker import maintain
    from course_platform.production.prompts import build_connection_request
    r = setup_run(jobs_context, kind="connection_test")
    claim = r.c.repository.claim_next("lost-worker")
    with transaction(r.c.db_path, immediate=True) as conn:
        call_id = r.c.budget.begin_call_in_tx(conn, claim, build_connection_request(claim.config))
    r.c.clock.advance(seconds=120)
    assert r.c.repository.mark_interrupted() == 1
    assert maintain(r.c.repository, r.c.budget) == 0
    assert maintain(r.c.repository, r.c.budget) == 0
    assert read_rows("SELECT status FROM model_calls WHERE id=?", (call_id,))[0]["status"] == "unknown"
    reserve = read_rows("SELECT * FROM token_reservations WHERE call_id=?", (call_id,))[0]
    assert reserve["used_tokens"] is None and reserve["released_tokens"] == 0
    assert not r.model.calls


def test_valid_precise_confirmation_allows_batch_without_reforging_confirmation(jobs_context, read_rows):
    r = setup_run(jobs_context, existing=True, confirmed=True, mode="precise", kind="chapters_generate")
    confirmation = read_rows("SELECT * FROM outline_confirmations")
    asyncio.run(worker(r).run_once())
    assert r.c.service.get(r.job.id).state == "succeeded"
    assert len(r.model.calls) == 2
    assert read_rows("SELECT * FROM outline_confirmations") == confirmation


@pytest.mark.parametrize("text", ["", "{\"items\":[]}", "invalid-json", "x" * 204801],
                         ids=["empty", "wrong-count", "invalid-json", "oversized"])
def test_invalid_outline_preserves_bounded_history_and_never_starts_chapters(jobs_context, read_rows, text):
    r = setup_run(jobs_context, model=ScriptedModel([result(text), result()]))
    asyncio.run(worker(r).run_once())
    assert r.c.service.get(r.job.id).state == "failed" and len(r.model.calls) == 1
    assert r.c.drafts.freeze_input(r.p.id).outline is None
    assert not read_rows("SELECT * FROM outline_revisions")
    call = read_rows("SELECT * FROM model_calls")[0]
    assert call["status"] == "successful" and call["input_tokens"] == 12
    assert call["response_text"] == (None if len(text.encode()) > 204800 else text)
