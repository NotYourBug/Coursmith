"""Explicit recovery contracts over real storage and controlled in-flight models."""

import asyncio
import json

import pytest

from course_platform.database import transaction
from course_platform.domain import BusinessError
from course_platform.production.llm import ModelFailure, Usage
from course_platform.production.models import BriefInput, OutlineInput, OutlineItem, ReferenceInput
from tests.jobs.fakes import ScriptedModel
from tests.jobs.test_worker import BODY, outline, result, setup_run, worker


def recovery(run):
    from course_platform.jobs.recovery import RecoveryService
    c = run.c
    return RecoveryService(c.db_path, c.service, c.drafts, c.budget, clock=c.clock.now)


def interrupted(run):
    claim = run.c.repository.claim_next("lost-worker")
    run.c.clock.advance(seconds=120)
    from course_platform.jobs.worker import maintain
    assert maintain(run.c.repository, run.c.budget) == 1
    return claim


def failed_partial(jobs_context, *, quick=False, bodies=False):
    script = ([outline(2)] if quick else []) + [result(), ModelFailure("not_sent", False)] * 1
    # A final proven rejection does not create unknown expense or successful work.
    script += [ModelFailure("not_sent", False), ModelFailure("not_sent", False)]
    r = setup_run(jobs_context, existing=not quick, bodies=bodies,
                  kind="course_draft_generate" if quick else "chapters_generate",
                  model=ScriptedModel(script))
    from unittest.mock import patch
    async def no_wait(seconds):
        pass
    with patch("course_platform.jobs.handlers.sleep", no_wait):
        asyncio.run(worker(r).run_once())
    assert r.c.service.get(r.job.id).state == "failed"
    return r


def resume(r, *, key="recover-1", revision=None, confirm=True, amount=0, config=None):
    return recovery(r).resume(r.c.actor, r.job.id,
        r.c.service.get(r.job.id).revision if revision is None else revision,
        r.c.config if config is None else config, idempotency_key=key,
        reserved_tokens=amount, confirm=confirm)


def unknown_run(jobs_context, *, key="test-key"):
    from types import SimpleNamespace
    c = jobs_context()
    p = c.project(outline=True)
    r = SimpleNamespace(c=c, p=p, job=c.submit(p, kind="chapters_generate", key=key),
        model=ScriptedModel([ModelFailure("response_unknown", None)]))
    asyncio.run(worker(r).run_once())
    assert r.c.service.get(r.job.id).state == "failed"
    return r


def failed_recovery_child(jobs_context):
    from types import SimpleNamespace
    parent = setup_run(jobs_context, existing=True, bodies=True, kind="chapters_generate")
    interrupted(parent)
    child = resume(parent)
    model = ScriptedModel([result(), ModelFailure("response_unknown", None)])
    leaf = SimpleNamespace(c=parent.c, p=parent.p, job=child, model=model)
    asyncio.run(worker(leaf).run_once())
    assert leaf.c.service.get(child.id).state == "failed"
    return parent, leaf


def recovery_history(read_rows):
    return {table: read_rows(f"SELECT * FROM {table} ORDER BY id") for table in (
        "production_jobs", "job_attempts", "job_steps", "model_calls", "token_reservations",
        "job_requests", "chapter_revisions", "production_candidates", "production_chapters")}


def test_superseded_ancestor_cannot_bypass_child_unknown_or_repeat_candidate(jobs_context, read_rows):
    parent, leaf = failed_recovery_child(jobs_context)
    ids = tuple(c.id for c in leaf.c.drafts.freeze_input(leaf.p.id).chapters)
    preview = recovery(leaf).recovery_preview(leaf.job.id)
    assert preview.completed_chapter_ids == (ids[0],)
    assert preview.pending_chapter_ids == (ids[1],) and len(preview.unknown_call_ids) == 1
    assert read_rows("SELECT chapter_id FROM production_candidates") == [{"chapter_id": ids[0]}]
    unknown = read_rows("SELECT * FROM model_calls WHERE status='unknown'")[0]
    reserve = read_rows("SELECT * FROM token_reservations WHERE call_id=?", (unknown["id"],))[0]
    assert reserve["reserved_tokens"] > 0 and reserve["released_tokens"] == 0
    assert read_rows("SELECT * FROM job_requests WHERE action='reconcile'") == []
    before = recovery_history(read_rows)
    assert resume(parent).id == leaf.job.id
    with pytest.raises(BusinessError) as unresolved:
        resume(leaf, key="leaf-unresolved")
    assert unresolved.value.code == "model_usage_unknown"
    with pytest.raises(BusinessError) as superseded:
        resume(parent, key="ancestor-branch")
    assert superseded.value.code == "recovery_superseded" and superseded.value.status_code == 409
    assert recovery_history(read_rows) == before
    assert asyncio.run(worker(leaf).run_once()) is False
    assert len(leaf.model.calls) == 2
    assert read_rows("SELECT * FROM model_calls WHERE id=?", (unknown["id"],))[0] == unknown
    assert read_rows("SELECT * FROM token_reservations WHERE call_id=?", (unknown["id"],))[0] == reserve


def test_reconciled_leaf_continues_only_pending_chapter_and_replays_ancestors_readonly(jobs_context, read_rows):
    parent, leaf = failed_recovery_child(jobs_context)
    ids = tuple(c.id for c in leaf.c.drafts.freeze_input(leaf.p.id).chapters)
    call = read_rows("SELECT * FROM model_calls WHERE job_id=? AND status='unknown'", (leaf.job.id,))[0]
    reserve = read_rows("SELECT * FROM token_reservations WHERE call_id=?", (call["id"],))[0]
    source_a = read_rows("SELECT * FROM chapter_revisions WHERE chapter_id=?", (ids[0],))
    candidate_a = read_rows("SELECT * FROM production_candidates WHERE chapter_id=?", (ids[0],))
    root_row = read_rows("SELECT * FROM production_jobs WHERE id=?", (parent.job.id,))[0]
    recovery(leaf).reconcile(leaf.c.actor, leaf.job.id, call["id"], leaf.c.service.get(leaf.job.id).revision,
        usage=None, accept_retry_risk=True)
    assert read_rows("SELECT * FROM model_calls WHERE id=?", (call["id"],))[0] == call
    assert read_rows("SELECT * FROM token_reservations WHERE call_id=?", (call["id"],))[0] == reserve
    assert recovery(leaf).recovery_preview(leaf.job.id).unknown_call_ids == (call["id"],)
    grandchild = resume(leaf, key="continue-leaf")
    row = read_rows("SELECT * FROM production_jobs WHERE id=?", (grandchild.id,))[0]
    assert grandchild.id not in (parent.job.id, leaf.job.id) and grandchild.state == "queued"
    assert row["parent_job_id"] == leaf.job.id and json.loads(row["chapter_ids_json"]) == [ids[1]]
    pools = read_rows("SELECT * FROM token_reservations WHERE call_id IS NULL ORDER BY id")
    assert len(pools) == 3 and len({pool["id"] for pool in pools}) == 3
    assert pools[-1]["job_id"] == grandchild.id and pools[-1]["reserved_tokens"] > 0
    before = recovery_history(read_rows)
    assert resume(parent).id == leaf.job.id
    assert resume(leaf, key="continue-leaf").id == grandchild.id
    for ancestor in (parent, leaf):
        with pytest.raises(BusinessError) as denied:
            resume(ancestor, key=f"branch-{ancestor.job.id}")
        assert denied.value.code == "recovery_superseded"
    assert recovery_history(read_rows) == before
    leaf.model = ScriptedModel([result()])
    asyncio.run(worker(leaf).run_once())
    assert leaf.c.service.get(grandchild.id).state == "succeeded"
    assert len(leaf.model.calls) == 1
    assert read_rows("SELECT chapter_id FROM job_steps WHERE job_id=? AND kind='chapter'", (grandchild.id,)) == [
        {"chapter_id": ids[1]}]
    tokens = read_rows("SELECT attempt_token FROM job_attempts ORDER BY id")
    assert len(tokens) == 3 and len({row["attempt_token"] for row in tokens}) == 3
    assert read_rows("SELECT * FROM production_jobs WHERE id=?", (parent.job.id,))[0] == root_row
    assert read_rows("SELECT * FROM chapter_revisions WHERE chapter_id=?", (ids[0],)) == source_a
    assert read_rows("SELECT * FROM production_candidates WHERE chapter_id=?", (ids[0],)) == candidate_a
    assert read_rows("SELECT * FROM model_calls WHERE id=?", (call["id"],))[0] == call
    assert read_rows("SELECT * FROM token_reservations WHERE call_id=?", (call["id"],))[0] == reserve
    before = recovery_history(read_rows)
    for ancestor in (parent, leaf):
        with pytest.raises(BusinessError) as denied:
            resume(ancestor, key=f"completed-branch-{ancestor.job.id}")
        assert denied.value.code == "recovery_superseded"
    assert resume(parent).id == leaf.job.id
    assert resume(leaf, key="continue-leaf").id == grandchild.id
    assert recovery_history(read_rows) == before


def test_superseded_parent_still_allows_historical_unknown_reconciliation(jobs_context, read_rows):
    r = unknown_run(jobs_context)
    svc = recovery(r)
    call = read_rows("SELECT * FROM model_calls WHERE status='unknown'")[0]
    svc.reconcile(r.c.actor, r.job.id, call["id"], r.c.service.get(r.job.id).revision,
        usage=None, accept_retry_risk=True)
    child = resume(r)
    child_history = read_rows("SELECT * FROM production_jobs WHERE id=?", (child.id,))
    parent_state = r.c.service.get(r.job.id).state
    requests = read_rows("SELECT * FROM job_requests WHERE action='resume'")
    svc.reconcile(r.c.actor, r.job.id, call["id"], r.c.service.get(r.job.id).revision,
        usage=Usage(6, 4), accept_retry_risk=False)
    assert read_rows("SELECT status,input_tokens,output_tokens FROM model_calls WHERE id=?", (call["id"],)) == [
        {"status": "unknown", "input_tokens": 6, "output_tokens": 4}]
    assert r.c.service.get(r.job.id).state == parent_state == "failed"
    assert read_rows("SELECT * FROM production_jobs WHERE id=?", (child.id,)) == child_history
    assert read_rows("SELECT * FROM job_requests WHERE action='resume'") == requests
    assert len(read_rows("SELECT * FROM job_requests WHERE action='reconcile'")) == 2
    reserve = read_rows("SELECT * FROM token_reservations WHERE call_id=?", (call["id"],))[0]
    assert reserve["used_tokens"] == 10 and reserve["released_tokens"] == reserve["reserved_tokens"] - 10
    before = recovery_history(read_rows)
    with pytest.raises(BusinessError) as denied:
        resume(r, key="reconciled-parent-branch")
    assert denied.value.code == "recovery_superseded"
    assert recovery_history(read_rows) == before


def test_superseded_ancestor_requires_current_owner_and_revision_before_denial_or_replay(jobs_context, read_rows):
    r = setup_run(jobs_context, existing=True, kind="chapters_generate")
    interrupted(r)
    child = resume(r)
    before = recovery_history(read_rows)
    revision = r.c.service.get(r.job.id).revision
    for key in ("recover-1", "new-branch"):
        for stale_revision in (True, str(revision), revision - 1):
            with pytest.raises(BusinessError) as stale:
                resume(r, key=key, revision=stale_revision)
            assert stale.value.code == "stale_revision"
    assert resume(r).id == child.id
    with transaction(r.c.db_path, immediate=True) as conn:
        conn.execute("UPDATE admins SET enabled=0 WHERE id=?", (r.c.actor.admin_id,))
    for key in ("recover-1", "new-branch"):
        for submitted_revision in (revision, revision - 1):
            with pytest.raises(BusinessError) as forbidden:
                resume(r, key=key, revision=submitted_revision)
            assert forbidden.value.code == "owner_required"
    assert recovery_history(read_rows) == before
    assert r.model.calls == []


@pytest.mark.parametrize("kind", ["chapter_regenerate", "draft_validate", "connection_test"])
def test_concurrent_distinct_recovery_keys_create_only_one_child(jobs_context, read_rows, kind):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    r = setup_run(jobs_context, existing=True, count=1, kind=kind)
    recovery(r).cancel(r.c.actor, r.job.id, r.job.revision)
    parent = read_rows("SELECT * FROM production_jobs WHERE id=?", (r.job.id,))[0]
    gate = Barrier(2)
    def submit(key):
        gate.wait(timeout=5)
        try:
            return resume(r, key=key)
        except BusinessError as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, ("tab-a", "tab-b")))
    children = [outcome for outcome in outcomes if not isinstance(outcome, BusinessError)]
    denials = [outcome for outcome in outcomes if isinstance(outcome, BusinessError)]
    assert len(children) == 1 and len(denials) == 1
    assert denials[0].code == "recovery_superseded"
    assert read_rows("SELECT id,parent_job_id,kind FROM production_jobs WHERE parent_job_id=?", (r.job.id,)) == [
        {"id": children[0].id, "parent_job_id": r.job.id, "kind": kind}]
    assert len(read_rows("SELECT * FROM production_jobs")) == 2
    assert len(read_rows("SELECT * FROM token_reservations WHERE call_id IS NULL")) == 2
    assert len(read_rows("SELECT * FROM job_requests WHERE action='resume'")) == 1
    assert read_rows("SELECT * FROM production_jobs WHERE id=?", (r.job.id,))[0] == parent
    assert read_rows("SELECT * FROM model_calls") == [] and read_rows("SELECT * FROM job_steps") == []
    assert r.model.calls == []


def test_cancel_queued_makes_zero_calls(jobs_context, read_rows):
    r = setup_run(jobs_context)
    view = recovery(r).cancel(r.c.actor, r.job.id, r.job.revision)
    assert view.state == "cancelled" and view.revision == 2
    assert asyncio.run(worker(r).run_once()) is False
    assert r.model.calls == [] and read_rows("SELECT * FROM model_calls") == []
    pool = read_rows("SELECT * FROM token_reservations")[0]
    assert pool["state"] == "released" and pool["released_tokens"] == pool["reserved_tokens"]
    assert read_rows("SELECT action FROM job_requests ORDER BY id") == [{"action": "submit"}, {"action": "cancel"}]


def test_cancel_inflight_stops_next_step_and_keeps_charged_candidate(jobs_context, read_rows):
    r = setup_run(jobs_context, existing=True, kind="chapters_generate")
    svc = recovery(r)
    async def scenario():
        barrier = asyncio.Event()
        r.model = ScriptedModel([result(), result()], barrier=barrier)
        task = asyncio.create_task(worker(r).run_once())
        await r.model.sent.wait()
        current = r.c.service.get(r.job.id)
        flagged = await asyncio.to_thread(svc.cancel, r.c.actor, r.job.id, current.revision)
        assert flagged.state == "running"
        barrier.set()
        await task
    asyncio.run(scenario())
    assert len(r.model.calls) == 1 and r.c.service.get(r.job.id).state == "cancelled"
    frozen = r.c.drafts.freeze_input(r.p.id)
    assert all(c.body_revision is None for c in frozen.chapters)
    candidates = read_rows("SELECT * FROM production_candidates")
    step = read_rows("SELECT * FROM job_steps")[0]
    assert len(candidates) == 1 and step["state"] == "succeeded" and step["candidate_id"] == candidates[0]["id"]
    assert read_rows("SELECT status,input_tokens,output_tokens FROM model_calls") == [
        {"status": "successful", "input_tokens": 12, "output_tokens": 4}]
    preview = svc.recovery_preview(r.job.id)
    assert preview.completed_chapter_ids == (frozen.chapters[0].id,)
    assert preview.pending_chapter_ids == (frozen.chapters[1].id,)


@pytest.mark.parametrize("quick,bodies", [(False, False), (True, False), (False, True)])
def test_resume_new_job_skips_successful_steps(jobs_context, read_rows, quick, bodies):
    r = failed_partial(jobs_context, quick=quick, bodies=bodies)
    parent = read_rows("SELECT * FROM production_jobs WHERE id=?", (r.job.id,))[0]
    frozen = r.c.drafts.freeze_input(r.p.id)
    successful_source = read_rows("SELECT * FROM chapter_revisions WHERE chapter_id=?", (frozen.chapters[0].id,))
    preview = recovery(r).recovery_preview(r.job.id)
    assert preview.unchanged_input is True
    assert preview.completed_chapter_ids == (frozen.chapters[0].id,)
    assert preview.pending_chapter_ids == (frozen.chapters[1].id,)
    child = resume(r, amount=1)
    row = read_rows("SELECT * FROM production_jobs WHERE id=?", (child.id,))[0]
    assert child.id != r.job.id and child.state == "queued" and row["parent_job_id"] == r.job.id
    assert row["kind"] == "chapters_generate" and json.loads(row["chapter_ids_json"]) == [frozen.chapters[1].id]
    assert read_rows("SELECT * FROM production_jobs WHERE id=?", (r.job.id,))[0] == parent
    assert read_rows("SELECT reserved_tokens FROM token_reservations WHERE job_id=? AND call_id IS NULL", (child.id,))[0]["reserved_tokens"] > 1
    r.model = ScriptedModel([result()])
    asyncio.run(worker(r).run_once())
    assert len(r.model.calls) == 1 and r.c.service.get(child.id).state == "succeeded"
    tokens = read_rows("SELECT attempt_token FROM job_attempts ORDER BY id")
    assert len(tokens) == 2 and tokens[0] != tokens[1]
    assert read_rows("SELECT * FROM chapter_revisions WHERE chapter_id=?", (frozen.chapters[0].id,)) == successful_source


@pytest.mark.parametrize("change", ["body", "brief", "reference", "outline", "mode"])
def test_changed_input_requires_new_submission_not_fake_resume(jobs_context, read_rows, change):
    r = failed_partial(jobs_context)
    frozen = r.c.drafts.freeze_input(r.p.id)
    p = r.c.projects.get(r.p.id)
    if change == "body":
        c = frozen.chapters[0]
        r.c.drafts.save_chapter(r.c.actor, p.id, c.id, c.body_revision, "人工修改")
    elif change == "brief":
        r.c.projects.update(r.c.actor, p.id, p.revision, BriefInput(title="新题目", chapter_count=2))
    elif change == "reference":
        r.c.projects.save_reference(r.c.actor, p.id, p.revision, None, ReferenceInput(text="新资料"))
    elif change == "outline":
        r.c.drafts.save_outline(r.c.actor, p.id, p.revision, OutlineInput(items=[
            OutlineItem(chapter_id=c.id, title="改名", goal="目标") for c in frozen.chapters]))
    else:
        r.c.projects.set_mode(r.c.actor, p.id, p.revision, "precise")
    assert recovery(r).recovery_preview(r.job.id).unchanged_input is False
    with pytest.raises(BusinessError) as denied:
        resume(r)
    assert denied.value.code == "input_changed" and denied.value.status_code == 409
    assert len(read_rows("SELECT * FROM production_jobs")) == 1


def test_accept_retry_risk_preserves_original_unknown_and_reservation(jobs_context, read_rows):
    r = unknown_run(jobs_context)
    svc = recovery(r)
    call = read_rows("SELECT * FROM model_calls")[0]
    reserve = read_rows("SELECT * FROM token_reservations WHERE call_id=?", (call["id"],))[0]
    before = (call, reserve)
    with pytest.raises(BusinessError) as denied:
        resume(r)
    assert denied.value.code == "model_usage_unknown"
    svc.reconcile(r.c.actor, r.job.id, call["id"], r.c.service.get(r.job.id).revision,
                  usage=None, accept_retry_risk=True)
    assert (read_rows("SELECT * FROM model_calls")[0],
            read_rows("SELECT * FROM token_reservations WHERE call_id=?", (call["id"],))[0]) == before
    assert svc.recovery_preview(r.job.id).unknown_call_ids == (call["id"],)
    record = read_rows("SELECT * FROM job_requests WHERE action='reconcile'")[0]
    assert record["accept_retry_risk"] == 1 and record["input_tokens"] is None
    child = resume(r)
    assert child.id != r.job.id
    assert read_rows("SELECT * FROM token_reservations WHERE call_id=?", (call["id"],))[0] == reserve
    r.model = ScriptedModel([result(), result()])
    asyncio.run(worker(r).run_once())
    assert r.c.service.get(child.id).state == "succeeded" and len(r.model.calls) == 2
    assert read_rows("SELECT * FROM model_calls WHERE id=?", (call["id"],))[0] == call
    assert read_rows("SELECT * FROM token_reservations WHERE call_id=?", (call["id"],))[0] == reserve


def test_reconcile_known_usage_preserves_unknown_and_repeated_request_does_not_overwrite(jobs_context, read_rows):
    r = unknown_run(jobs_context)
    svc = recovery(r)
    call_id = read_rows("SELECT id FROM model_calls")[0]["id"]
    old_revision = r.c.service.get(r.job.id).revision
    svc.reconcile(r.c.actor, r.job.id, call_id, old_revision, usage=Usage(7, 3), accept_retry_risk=False)
    assert read_rows("SELECT status,input_tokens,output_tokens FROM model_calls") == [
        {"status": "unknown", "input_tokens": 7, "output_tokens": 3}]
    reserve = read_rows("SELECT * FROM token_reservations WHERE call_id=?", (call_id,))[0]
    assert reserve["used_tokens"] == 10 and reserve["released_tokens"] == reserve["reserved_tokens"] - 10
    with pytest.raises(BusinessError) as stale:
        svc.reconcile(r.c.actor, r.job.id, call_id, old_revision, usage=Usage(1, 1), accept_retry_risk=False)
    assert stale.value.code == "stale_revision"
    svc.reconcile(r.c.actor, r.job.id, call_id, r.c.service.get(r.job.id).revision,
                  usage=Usage(7, 3), accept_retry_risk=False)
    assert read_rows("SELECT * FROM token_reservations WHERE call_id=?", (call_id,))[0] == reserve
    with pytest.raises(BusinessError) as conflict:
        svc.reconcile(r.c.actor, r.job.id, call_id, r.c.service.get(r.job.id).revision,
                      usage=Usage(1, 1), accept_retry_risk=False)
    assert conflict.value.code == "settlement_conflict"
    assert resume(r).state == "queued"


def test_foreign_call_reconciliation_is_rejected(jobs_context, read_rows):
    first = unknown_run(jobs_context)
    first_call = read_rows("SELECT id FROM model_calls")[0]["id"]
    second = unknown_run(jobs_context, key="foreign-second")
    before = read_rows("SELECT * FROM token_reservations")
    with pytest.raises(BusinessError) as denied:
        recovery(second).reconcile(second.c.actor, second.job.id, first_call,
            second.c.service.get(second.job.id).revision, usage=Usage(0, 0), accept_retry_risk=True)
    assert denied.value.code == "model_call_missing"
    assert read_rows("SELECT * FROM token_reservations") == before


def test_preview_is_readonly_and_does_not_maintain_lost_worker(jobs_context, read_rows):
    r = setup_run(jobs_context)
    r.c.repository.claim_next("lost-worker")
    r.c.clock.advance(seconds=120)
    svc = recovery(r)
    before = {table: read_rows(f"SELECT * FROM {table}") for table in (
        "production_jobs", "job_attempts", "job_steps", "token_reservations", "job_requests", "admin_events")}
    assert svc.recovery_preview(r.job.id).unchanged_input is True
    assert before == {table: read_rows(f"SELECT * FROM {table}") for table in before}
    assert r.model.calls == []


def test_cancel_explicitly_maintains_unknown_ledger_after_worker_loss(jobs_context, read_rows):
    r = setup_run(jobs_context, existing=True, kind="chapters_generate")
    svc = recovery(r)
    claim = r.c.repository.claim_next("lost-worker")
    from course_platform.production.prompts import build_chapter_request
    with transaction(r.c.db_path, immediate=True) as conn:
        r.c.budget.begin_call_in_tx(conn, claim, build_chapter_request(claim.input, claim.input.chapters[0].id, claim.config))
    r.c.clock.advance(seconds=120)
    with pytest.raises(BusinessError) as stale:
        svc.cancel(r.c.actor, r.job.id, r.c.service.get(r.job.id).revision)
    assert stale.value.code == "stale_revision"
    assert r.c.service.get(r.job.id).state == "interrupted"
    assert read_rows("SELECT status FROM model_calls") == [{"status": "unknown"}]
    assert read_rows("SELECT released_tokens FROM token_reservations WHERE call_id IS NOT NULL") == [{"released_tokens": 0}]


@pytest.mark.parametrize("operation", ["cancel", "resume", "reconcile"])
def test_current_owner_required_before_action(jobs_context, read_rows, operation):
    r = unknown_run(jobs_context)
    svc = recovery(r)
    call = read_rows("SELECT id FROM model_calls")[0]["id"]
    revision = r.c.service.get(r.job.id).revision
    with transaction(r.c.db_path, immediate=True) as conn:
        conn.execute("UPDATE admins SET enabled=0 WHERE id=?", (r.c.actor.admin_id,))
    with pytest.raises(BusinessError) as denied:
        if operation == "cancel":
            svc.cancel(r.c.actor, r.job.id, revision)
        elif operation == "resume":
            svc.resume(r.c.actor, r.job.id, revision, r.c.config, idempotency_key="owner", reserved_tokens=0, confirm=True)
        else:
            svc.reconcile(r.c.actor, r.job.id, call, revision, usage=None, accept_retry_risk=True)
    assert denied.value.code == "owner_required"
    assert read_rows("SELECT * FROM job_requests WHERE action!='submit'") == []


@pytest.mark.parametrize("operation", ["cancel", "resume", "reconcile"])
def test_strict_stale_revision_rejected(jobs_context, read_rows, operation):
    r = unknown_run(jobs_context)
    svc = recovery(r)
    current = r.c.service.get(r.job.id)
    call = read_rows("SELECT id FROM model_calls")[0]["id"]
    for revision in (True, str(current.revision), current.revision - 1):
        with pytest.raises(BusinessError) as stale:
            if operation == "cancel":
                svc.cancel(r.c.actor, r.job.id, revision)
            elif operation == "resume":
                resume(r, revision=revision)
            else:
                svc.reconcile(r.c.actor, r.job.id, call, revision, usage=None, accept_retry_risk=True)
        assert stale.value.code == "stale_revision"


def test_resume_confirmation_idempotency_and_new_reservation(jobs_context, read_rows):
    r = setup_run(jobs_context, existing=True, kind="chapters_generate")
    interrupted(r)
    for confirm in (False, 1, "yes"):
        with pytest.raises(BusinessError) as denied:
            resume(r, confirm=confirm)
        assert denied.value.code == "recovery_confirmation_required"
    first = resume(r, amount=0)
    assert resume(r, amount=999).id == first.id
    with pytest.raises(BusinessError) as conflict:
        resume(r, config=r.c.config.model_copy(update={"prompt_version": "v2"}))
    assert conflict.value.code == "idempotency_conflict"
    with transaction(r.c.db_path, immediate=True) as conn:
        conn.execute("UPDATE admins SET enabled=0 WHERE id=?", (r.c.actor.admin_id,))
    with pytest.raises(BusinessError) as forbidden:
        resume(r)
    assert forbidden.value.code == "owner_required"
    assert len(read_rows("SELECT * FROM production_jobs")) == 2


def test_parent_job_cannot_bypass_explicit_recovery_service(jobs_context):
    r = unknown_run(jobs_context)
    p = r.c.projects.get(r.p.id)
    with pytest.raises(BusinessError) as denied:
        r.c.service.submit(r.c.actor, r.c.request(kind="chapters_generate", project_id=p.id,
            project_revision=p.revision, parent_job_id=r.job.id), r.c.config,
            idempotency_key="bypass", reserved_tokens=0)
    assert denied.value.code == "explicit_recovery_required"


def test_cancelled_outline_candidate_cannot_fake_completed_course(jobs_context, read_rows):
    r = setup_run(jobs_context)
    svc = recovery(r)
    async def scenario():
        barrier = asyncio.Event()
        r.model = ScriptedModel([outline(2)], barrier=barrier)
        task = asyncio.create_task(worker(r).run_once())
        await r.model.sent.wait()
        await asyncio.to_thread(svc.cancel, r.c.actor, r.job.id, r.c.service.get(r.job.id).revision)
        barrier.set()
        await task
    asyncio.run(scenario())
    assert r.c.drafts.freeze_input(r.p.id).outline is None
    assert recovery(r).recovery_preview(r.job.id).pending_chapter_ids == ()
    with pytest.raises(BusinessError) as denied:
        resume(r)
    assert denied.value.code == "recovery_candidate_pending"
    assert len(read_rows("SELECT * FROM production_jobs")) == 1


def test_selected_original_goal_not_all_outline_chapters(jobs_context, read_rows):
    c = jobs_context()
    p = c.project(outline=True)
    ids = tuple(ch.id for ch in c.drafts.freeze_input(p.id).chapters)
    job = c.submit(p, kind="chapter_regenerate", chapter_ids=(ids[1],))
    from types import SimpleNamespace
    r = SimpleNamespace(c=c, p=p, job=job, model=ScriptedModel([]))
    interrupted(r)
    preview = recovery(r).recovery_preview(job.id)
    assert preview.pending_chapter_ids == (ids[1],)
    child = resume(r)
    assert read_rows("SELECT kind,chapter_ids_json FROM production_jobs WHERE id=?", (child.id,)) == [
        {"kind": "chapter_regenerate", "chapter_ids_json": json.dumps([ids[1]], separators=(",", ":"))}]


def test_completed_chapters_resume_only_original_validation(jobs_context, read_rows, monkeypatch):
    from course_platform.jobs.handlers import Handlers
    r = setup_run(jobs_context, existing=True, kind="chapters_generate")
    def crash_validate(self):
        raise BusinessError("storage_failure", "检查未完成。", 409)
    monkeypatch.setattr(Handlers, "_validate", crash_validate)
    asyncio.run(worker(r).run_once())
    assert len(r.model.calls) == 2
    preview = recovery(r).recovery_preview(r.job.id)
    assert preview.pending_chapter_ids == () and len(preview.completed_chapter_ids) == 2
    child = resume(r)
    assert read_rows("SELECT kind FROM production_jobs WHERE id=?", (child.id,)) == [{"kind": "draft_validate"}]


def test_completed_terminal_job_has_nothing_to_resume(jobs_context, read_rows):
    r = setup_run(jobs_context, existing=True, count=1, kind="chapter_regenerate")
    asyncio.run(worker(r).run_once())
    with pytest.raises(BusinessError) as denied:
        resume(r)
    assert denied.value.code == "recovery_not_needed"
    assert len(read_rows("SELECT * FROM production_jobs")) == 1


@pytest.mark.parametrize("operation", ["cancel", "reconcile", "resume"])
def test_recovery_success_audit_failure_rolls_back_all_business_writes(jobs_context, read_rows, monkeypatch, operation):
    r = unknown_run(jobs_context) if operation == "reconcile" else setup_run(jobs_context, existing=True, kind="chapters_generate")
    svc = recovery(r)
    if operation == "resume":
        interrupted(r)
    from course_platform.jobs import recovery as module
    before = {table: read_rows(f"SELECT * FROM {table}") for table in (
        "production_jobs", "model_calls", "token_reservations", "job_requests")}
    def fail_event(*args):
        raise BusinessError("audit_payload", "审计失败。", 400)
    monkeypatch.setattr(module, "append_event", fail_event)
    with pytest.raises(BusinessError):
        if operation == "cancel":
            svc.cancel(r.c.actor, r.job.id, r.c.service.get(r.job.id).revision)
        elif operation == "resume":
            resume(r)
        else:
            call = read_rows("SELECT id FROM model_calls")[0]["id"]
            svc.reconcile(r.c.actor, r.job.id, call, r.c.service.get(r.job.id).revision,
                          usage=Usage(2, 3), accept_retry_risk=False)
    assert before == {table: read_rows(f"SELECT * FROM {table}") for table in before}


def test_recovery_errors_and_audit_never_contain_private_sources(jobs_context, read_rows):
    r = failed_partial(jobs_context)
    svc = recovery(r)
    chapter = r.c.drafts.freeze_input(r.p.id).chapters[0]
    sentinel = "private-recovery-body-中文"
    r.c.drafts.save_chapter(r.c.actor, r.p.id, chapter.id, chapter.body_revision, sentinel)
    with pytest.raises(BusinessError) as denied:
        resume(r)
    assert sentinel not in str(denied.value)
    assert sentinel not in repr(svc.recovery_preview(r.job.id))
    assert sentinel not in json.dumps(read_rows("SELECT * FROM admin_events"), ensure_ascii=False)


@pytest.mark.parametrize("bad", [True, -1, "10", 9223372036854775808])
def test_reconcile_revalidates_usage_even_for_overridden_constructor(jobs_context, read_rows, bad):
    r = unknown_run(jobs_context)
    svc = recovery(r)
    class UnvalidatedUsage(Usage):
        def __post_init__(self):
            pass
    call = read_rows("SELECT * FROM model_calls")[0]
    before = read_rows("SELECT * FROM token_reservations")
    with pytest.raises(BusinessError):
        svc.reconcile(r.c.actor, r.job.id, call["id"], r.c.service.get(r.job.id).revision,
                      usage=UnvalidatedUsage(bad, 1), accept_retry_risk=False)
    assert read_rows("SELECT * FROM model_calls")[0] == call
    assert read_rows("SELECT * FROM token_reservations") == before


def test_risk_repeat_cannot_erase_later_known_usage(jobs_context, read_rows):
    r = unknown_run(jobs_context)
    svc = recovery(r)
    call = read_rows("SELECT id FROM model_calls")[0]["id"]
    svc.reconcile(r.c.actor, r.job.id, call, r.c.service.get(r.job.id).revision, usage=None, accept_retry_risk=True)
    svc.reconcile(r.c.actor, r.job.id, call, r.c.service.get(r.job.id).revision, usage=Usage(6, 4), accept_retry_risk=False)
    before = read_rows("SELECT * FROM token_reservations")
    svc.reconcile(r.c.actor, r.job.id, call, r.c.service.get(r.job.id).revision, usage=None, accept_retry_risk=True)
    assert read_rows("SELECT status,input_tokens,output_tokens FROM model_calls") == [
        {"status": "unknown", "input_tokens": 6, "output_tokens": 4}]
    assert read_rows("SELECT * FROM token_reservations") == before


def test_distinct_retry_key_continues_failed_child_after_zero_acceptance_failure(jobs_context, read_rows):
    r = setup_run(jobs_context, existing=True, kind="chapter_regenerate", count=1)
    interrupted(r)
    first = resume(r)
    from unittest.mock import patch
    async def no_wait(seconds):
        pass
    r.model = ScriptedModel([ModelFailure("not_sent", False)] * 3)
    with patch("course_platform.jobs.handlers.sleep", no_wait):
        asyncio.run(worker(r).run_once())
    assert r.c.service.get(first.id).state == "failed"
    before = recovery_history(read_rows)
    assert resume(r).id == first.id
    with pytest.raises(BusinessError) as superseded:
        resume(r, key="recover-2")
    assert superseded.value.code == "recovery_superseded"
    assert recovery_history(read_rows) == before
    from types import SimpleNamespace
    leaf = SimpleNamespace(c=r.c, p=r.p, job=first, model=r.model)
    second = resume(leaf, key="recover-2")
    assert second.id != first.id and second.state == "queued"
    assert read_rows("SELECT parent_job_id FROM production_jobs WHERE id=?", (second.id,)) == [{"parent_job_id": first.id}]
    assert len(read_rows("SELECT * FROM token_reservations WHERE call_id IS NULL")) == 3


def test_missing_result_source_proof_is_not_successful_work(jobs_context, read_rows):
    r = failed_partial(jobs_context)
    step = read_rows("SELECT * FROM job_steps WHERE state='succeeded'")[0]
    # Same-project stable chapter FK is valid; no source pointer is not proof.
    with transaction(r.c.db_path, immediate=True) as conn:
        conn.execute("UPDATE job_steps SET chapter_revision_id=NULL WHERE id=?", (step["id"],))
    with pytest.raises(BusinessError) as denied:
        recovery(r).recovery_preview(r.job.id)
    assert denied.value.code == "input_unverified"
    with pytest.raises(BusinessError):
        resume(r)
    assert len(read_rows("SELECT * FROM production_jobs")) == 1


def test_failed_job_with_complete_regeneration_does_not_invent_validation(jobs_context, monkeypatch):
    from course_platform.jobs.handlers import Handlers
    r = setup_run(jobs_context, existing=True, count=1, kind="chapter_regenerate")
    def fail_finish(self):
        raise BusinessError("storage_failure", "任务未写完终态。", 409)
    monkeypatch.setattr(Handlers, "_finish", fail_finish)
    asyncio.run(worker(r).run_once())
    assert r.c.service.get(r.job.id).state == "failed"
    preview = recovery(r).recovery_preview(r.job.id)
    assert len(preview.completed_chapter_ids) == 1 and preview.pending_chapter_ids == ()
    with pytest.raises(BusinessError) as denied:
        resume(r)
    assert denied.value.code == "recovery_not_needed"


def test_resume_precise_confirmation_cannot_be_bypassed_by_checkbox(jobs_context, read_rows):
    r = setup_run(jobs_context, mode="precise", existing=True, confirmed=True, kind="chapters_generate")
    interrupted(r)
    with transaction(r.c.db_path, immediate=True) as conn:
        conn.execute("UPDATE production_projects SET current_confirmation_id=NULL WHERE id=?", (r.p.id,))
    assert recovery(r).recovery_preview(r.job.id).unchanged_input is True
    with pytest.raises(BusinessError) as denied:
        resume(r)
    assert denied.value.code == "outline_unconfirmed"
    assert len(read_rows("SELECT * FROM production_jobs")) == 1


@pytest.mark.parametrize("kind,want_calls", [("course_draft_generate", 3), ("outline_generate", 1), ("connection_test", 1)])
def test_cancelled_unstarted_goal_resumes_without_missing_step_guess(jobs_context, read_rows, kind, want_calls):
    r = setup_run(jobs_context, kind=kind)
    svc = recovery(r)
    svc.cancel(r.c.actor, r.job.id, r.job.revision)
    assert read_rows("SELECT * FROM job_steps") == []
    child = resume(r)
    assert read_rows("SELECT kind,parent_job_id FROM production_jobs WHERE id=?", (child.id,)) == [
        {"kind": kind, "parent_job_id": r.job.id}]
    asyncio.run(worker(r).run_once())
    assert r.c.service.get(child.id).state == "succeeded" and len(r.model.calls) == want_calls
    assert r.c.service.get(r.job.id).state == "cancelled"


def test_archive_blocks_recovery_even_when_source_hash_unchanged(jobs_context):
    r = setup_run(jobs_context, existing=True, kind="chapters_generate")
    interrupted(r)
    p = r.c.projects.get(r.p.id)
    r.c.projects.archive(r.c.actor, p.id, p.revision)
    assert recovery(r).recovery_preview(r.job.id).unchanged_input is True
    with pytest.raises(BusinessError) as denied:
        resume(r)
    assert denied.value.code == "project_archived"


def test_known_successful_call_cannot_be_overwritten_by_reconciliation(jobs_context, read_rows):
    r = failed_partial(jobs_context)
    call = read_rows("SELECT * FROM model_calls WHERE status='successful'")[0]
    before = read_rows("SELECT * FROM token_reservations")
    with pytest.raises(BusinessError) as denied:
        recovery(r).reconcile(r.c.actor, r.job.id, call["id"], r.c.service.get(r.job.id).revision,
            usage=Usage(0, 0), accept_retry_risk=True)
    assert denied.value.code == "settlement_conflict"
    assert read_rows("SELECT * FROM model_calls WHERE id=?", (call["id"],))[0] == call
    assert read_rows("SELECT * FROM token_reservations") == before


@pytest.mark.parametrize("flag", [1, "yes", False])
def test_unknown_risk_requires_explicit_boolean_consent(jobs_context, read_rows, flag):
    r = unknown_run(jobs_context)
    call = read_rows("SELECT id FROM model_calls")[0]["id"]
    before = read_rows("SELECT * FROM token_reservations")
    with pytest.raises(BusinessError):
        recovery(r).reconcile(r.c.actor, r.job.id, call, r.c.service.get(r.job.id).revision,
            usage=None, accept_retry_risk=flag)
    assert read_rows("SELECT * FROM token_reservations") == before
    assert read_rows("SELECT * FROM job_requests WHERE action='reconcile'") == []


def test_active_job_cannot_create_child_by_recovery_checkbox(jobs_context, read_rows):
    r = setup_run(jobs_context)
    with pytest.raises(BusinessError) as denied:
        resume(r)
    assert denied.value.code == "invalid_job_state"
    assert len(read_rows("SELECT * FROM production_jobs")) == 1
