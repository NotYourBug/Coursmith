"""Persistent global claiming, token fences, heartbeat and interruption boundaries."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier

import pytest

from course_platform.database import transaction
from course_platform.domain import Actor, BusinessError


def test_worker_claim_is_exclusive_and_persistent(jobs_context, read_rows):
    j = jobs_context()
    job = j.submit(j.project())
    claim = j.repository.claim_next("worker")
    assert claim.job_id == job.id
    assert claim.actor.admin_id == j.actor.admin_id
    assert claim.input.project_id == j.drafts.freeze_input(claim.input.project_id).project_id
    assert claim.lease_until == j.clock.now() + timedelta(seconds=60)
    assert j.repository_type(j.db_path, clock=j.clock.now).claim_next("restarted-worker") is None
    assert j.service.get(job.id).state == "running"
    attempts = read_rows("SELECT * FROM job_attempts")
    assert len(attempts) == 1 and attempts[0]["attempt_number"] == 1
    assert attempts[0]["attempt_token"] == claim.attempt_token
    assert read_rows("SELECT * FROM worker_heartbeats")[0]["attempt_id"] == claim.attempt_id


def test_second_worker_cannot_run_other_project_simultaneously(jobs_context):
    j = jobs_context()
    first = j.submit(j.project(), key="first")
    second = j.submit(j.project(), key="second")
    claim = j.repository.claim_next("worker-one")
    assert claim.job_id == first.id
    assert j.repository.claim_next("worker-two") is None
    assert j.service.get(second.id).state == "queued"
    with transaction(j.db_path, immediate=True) as connection:
        j.repository.finish_in_tx(connection, claim, "succeeded", None)
    assert j.repository.claim_next("worker-two").job_id == second.id


def test_real_concurrent_claims_have_one_global_winner(jobs_context, read_rows):
    j = jobs_context()
    j.submit(j.project(), key="first")
    j.submit(j.project(), key="second")
    barrier = Barrier(2)

    def claim(worker):
        repository = j.repository_type(j.db_path, clock=j.clock.now)
        barrier.wait(timeout=5)
        return repository.claim_next(worker)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["one", "two"]))
    assert sum(result is not None for result in results) == 1
    assert len(read_rows("SELECT * FROM production_jobs WHERE state='running'")) == 1
    assert len(read_rows("SELECT * FROM job_attempts")) == 1


def test_lease_expiry_fences_at_60_seconds_but_interrupts_at_120(jobs_context, read_rows):
    j = jobs_context()
    job = j.submit(j.project())
    claim = j.repository.claim_next("worker")
    j.clock.advance(seconds=59)
    with transaction(j.db_path, immediate=True) as connection:
        assert j.repository.require_attempt_in_tx(connection, claim, j.clock.now()).state == "running"
    j.clock.advance(seconds=1)
    with pytest.raises(BusinessError) as denied:
        with transaction(j.db_path, immediate=True) as connection:
            j.repository.require_attempt_in_tx(connection, claim, j.clock.now())
    assert denied.value.code == "attempt_expired"
    assert j.repository.mark_interrupted() == 0
    assert j.repository.claim_next("other-worker") is None
    j.clock.advance(seconds=59)
    assert j.repository.mark_interrupted() == 0
    j.clock.advance(seconds=1)
    assert j.repository.mark_interrupted() == 1
    assert j.service.get(job.id).state == "interrupted"
    assert read_rows("SELECT * FROM job_attempts")[0]["state"] == "interrupted"
    assert j.repository.mark_interrupted() == 0
    assert j.repository.claim_next("restarted") is None
    assert read_rows("SELECT * FROM worker_heartbeats")[0]["job_id"] is None
    assert len(read_rows("SELECT * FROM admin_events WHERE action='job.interrupt'")) == 1


def test_heartbeat_extends_persisted_lease_without_trusting_claim_timestamp(jobs_context, read_rows):
    j = jobs_context()
    j.submit(j.project())
    claim = j.repository.claim_next("worker")
    j.clock.advance(seconds=50)
    assert j.repository.heartbeat(claim) is None
    row = read_rows("SELECT * FROM job_attempts")[0]
    assert row["lease_until"] == (j.clock.now() + timedelta(seconds=60)).isoformat()
    j.clock.advance(seconds=10)
    with transaction(j.db_path, immediate=True) as connection:
        assert j.repository.require_attempt_in_tx(connection, claim, j.clock.now()).state == "running"
    j.clock.advance(seconds=100)
    assert j.repository.mark_interrupted() == 0
    j.clock.advance(seconds=10)
    assert j.repository.mark_interrupted() == 1


@pytest.mark.parametrize("operation", ["heartbeat", "finish"])
def test_expired_attempt_cannot_renew_or_finish(jobs_context, read_rows, operation):
    j = jobs_context()
    j.submit(j.project())
    claim = j.repository.claim_next("worker")
    j.clock.advance(seconds=60)
    before = read_rows("SELECT * FROM job_attempts")
    with pytest.raises(BusinessError) as denied:
        if operation == "heartbeat":
            j.repository.heartbeat(replace(claim, lease_until=j.clock.now() + timedelta(days=1)))
        else:
            with transaction(j.db_path, immediate=True) as connection:
                j.repository.finish_in_tx(connection, claim, "succeeded", None)
    assert denied.value.code == "attempt_expired"
    assert read_rows("SELECT * FROM job_attempts") == before


@pytest.mark.parametrize("field,value", [
    ("attempt_token", "wrong-token"), ("job_id", 999), ("attempt_id", 999),
    ("actor", Actor(999, "test")), ("job_id", True),
])
def test_wrong_attempt_identity_fences_writes(jobs_context, field, value):
    j = jobs_context()
    j.submit(j.project())
    claim = replace(j.repository.claim_next("worker"), **{field:value})
    with pytest.raises(BusinessError) as denied:
        with transaction(j.db_path, immediate=True) as connection:
            j.repository.require_attempt_in_tx(connection, claim, j.clock.now())
    assert denied.value.code == "attempt_expired"


def test_require_attempt_and_finish_need_callers_transaction(jobs_context):
    j = jobs_context()
    j.submit(j.project())
    claim = j.repository.claim_next("worker")
    from course_platform.database import connect
    connection = connect(j.db_path)
    try:
        with pytest.raises(BusinessError) as denied:
            j.repository.require_attempt_in_tx(connection, claim, j.clock.now())
        assert denied.value.code == "job_transaction"
    finally:
        connection.close()


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled", "interrupted"])
def test_finished_job_and_old_token_never_run_again(jobs_context, read_rows, state):
    j = jobs_context()
    job = j.submit(j.project())
    claim = j.repository.claim_next("worker")
    with transaction(j.db_path, immediate=True) as connection:
        result = j.repository.finish_in_tx(connection, claim, state, None)
    assert result.state == state
    assert j.service.get(job.id).state == state
    assert read_rows("SELECT * FROM job_attempts")[0]["state"] == state
    assert j.repository.claim_next("worker") is None
    with pytest.raises(BusinessError) as denied:
        j.repository.heartbeat(claim)
    assert denied.value.code == "attempt_expired"


@pytest.mark.parametrize("state,reason", [("queued",None), ("running",None), ("unknown",None), ("failed","private text"), ("failed","CS-secret")])
def test_finish_rejects_invalid_state_and_raw_reason(jobs_context, read_rows, state, reason):
    j = jobs_context()
    job = j.submit(j.project())
    claim = j.repository.claim_next("worker")
    with pytest.raises(BusinessError) as denied:
        with transaction(j.db_path, immediate=True) as connection:
            j.repository.finish_in_tx(connection, claim, state, reason)
    assert denied.value.code == "invalid_job_state"
    assert j.service.get(job.id).state == "running"
    assert read_rows("SELECT * FROM job_attempts")[0]["state"] == "running"


def test_interruption_worker_filter_releases_project_slot(jobs_context):
    j = jobs_context()
    p = j.project()
    j.submit(p)
    j.repository.claim_next("original")
    j.clock.advance(seconds=120)
    assert j.repository.mark_interrupted(worker_id="different") == 0
    assert j.repository.mark_interrupted(worker_id="original") == 1
    assert j.submit(p, key="new-explicit-submission").state == "queued"


def test_enabled_owner_is_checked_at_claim_and_each_write(jobs_context):
    j = jobs_context()
    j.submit(j.project())
    with transaction(j.db_path, immediate=True) as connection:
        connection.execute("UPDATE admins SET enabled=0 WHERE id=1")
    with pytest.raises(BusinessError) as denied:
        j.repository.claim_next("worker")
    assert denied.value.code == "owner_required"
    with transaction(j.db_path, immediate=True) as connection:
        connection.execute("UPDATE admins SET enabled=1 WHERE id=1")
    claim = j.repository.claim_next("worker")
    with transaction(j.db_path, immediate=True) as connection:
        connection.execute("UPDATE admins SET enabled=0 WHERE id=1")
    with pytest.raises(BusinessError) as denied:
        j.repository.heartbeat(claim)
    assert denied.value.code == "owner_required"


def test_claim_uses_frozen_source_after_current_project_edit(jobs_context):
    j = jobs_context()
    p = j.project(outline=True)
    job = j.submit(p, kind="draft_validate")
    old = j.drafts.freeze_input(p.id)
    j.drafts.save_chapter(j.actor, p.id, old.chapters[0].id, None, "private-new-body")
    claim = j.repository.claim_next("worker")
    assert claim.job_id == job.id
    assert claim.input == old
    assert claim.input.chapters[0].body_revision is None
    assert "private-" not in repr(claim)
    assert claim.attempt_token not in repr(claim)


def test_connection_claim_has_no_project_input(jobs_context):
    j = jobs_context()
    job = j.submit(kind="connection_test")
    claim = j.repository.claim_next("worker")
    assert claim.job_id == job.id
    assert claim.input is None
    assert claim.config == j.config


def test_interruption_audit_failure_rolls_back_all_state(jobs_context, read_rows, monkeypatch):
    j = jobs_context()
    job = j.submit(j.project())
    j.repository.claim_next("worker")
    j.clock.advance(seconds=120)
    before = read_rows("SELECT * FROM job_attempts")

    def fail(*args):
        raise BusinessError("audit_unavailable", "safe", 503)
    monkeypatch.setattr("course_platform.jobs.repository.append_event", fail)
    with pytest.raises(BusinessError):
        j.repository.mark_interrupted()
    assert j.service.get(job.id).state == "running"
    assert read_rows("SELECT * FROM job_attempts") == before


def test_claim_rolls_back_on_failure_after_attempt_insert(jobs_context, read_rows, monkeypatch):
    j = jobs_context()
    job = j.submit(j.project())

    def fail(*args, **kwargs):
        raise BusinessError("input_unverified", "safe", 409)
    monkeypatch.setattr("course_platform.production.drafts.DraftService._job_payload", fail)
    with pytest.raises(BusinessError):
        j.repository.claim_next("worker")
    assert j.service.get(job.id).state == "queued"
    assert read_rows("SELECT * FROM job_attempts") == []
    assert read_rows("SELECT * FROM worker_heartbeats") == []
def test_connection_hash_is_verified_before_claim(jobs_context, read_rows):
    j = jobs_context()
    with transaction(j.db_path, immediate=True) as connection:
        connection.execute("""INSERT INTO production_jobs
            (actor_admin_id,kind,input_hash,input_json,config_json,request_digest)
            VALUES (?,'connection_test',?,'{}',?,'fixture-digest')""",
            (j.actor.admin_id, "b" * 64, j.config.model_dump_json()))
    with pytest.raises(BusinessError) as denied:
        j.repository.claim_next("worker")
    assert denied.value.code == "input_unverified"
    assert read_rows("SELECT state FROM production_jobs") == [{"state":"queued"}]
    assert read_rows("SELECT * FROM job_attempts") == []


def test_incomplete_job_cannot_finish_successfully(jobs_context):
    j = jobs_context()
    job = j.submit(j.project(outline=True), kind="chapters_generate")
    claim = j.repository.claim_next("worker")
    with transaction(j.db_path, immediate=True) as connection:
        connection.execute("""INSERT INTO job_steps
            (job_id,attempt_id,project_id,step_number,kind,chapter_id,input_hash,state)
            VALUES (?,?,?,?,?,?,?,'failed')""",
            (job.id, claim.attempt_id, claim.input.project_id, 1, "chapter",
             claim.input.chapters[0].id, claim.input.input_hash))
    with pytest.raises(BusinessError) as denied:
        with transaction(j.db_path, immediate=True) as connection:
            j.repository.finish_in_tx(connection, claim, "succeeded", None)
    assert denied.value.code == "invalid_job_state"
    assert j.service.get(job.id).state == "running"


def test_interruption_and_expired_heartbeat_race_cannot_resurrect(jobs_context, read_rows):
    j = jobs_context()
    job = j.submit(j.project())
    claim = j.repository.claim_next("worker")
    j.clock.advance(seconds=120)
    barrier = Barrier(2)

    def heartbeat():
        barrier.wait(timeout=5)
        try:
            j.repository.heartbeat(claim)
        except BusinessError as error:
            return error.code

    def maintain():
        barrier.wait(timeout=5)
        return j.repository_type(j.db_path, clock=j.clock.now).mark_interrupted()

    with ThreadPoolExecutor(max_workers=2) as pool:
        heartbeat_result = pool.submit(heartbeat)
        maintenance_result = pool.submit(maintain)
        assert heartbeat_result.result(timeout=10) == "attempt_expired"
        assert maintenance_result.result(timeout=10) == 1
    assert j.service.get(job.id).state == "interrupted"
    assert read_rows("SELECT state FROM job_attempts") == [{"state":"interrupted"}]


def test_private_validation_error_is_sanitized_when_claim_fails(jobs_context, read_rows):
    j = jobs_context()
    # Schema accepts JSON shape; repository still must reject extra private fields.
    config = j.config.model_dump() | {"api_key":"private-secret"}
    import json
    with transaction(j.db_path, immediate=True) as connection:
        connection.execute("""INSERT INTO production_jobs
            (project_id,actor_admin_id,kind,project_revision,input_hash,input_json,config_json,request_digest)
            VALUES (NULL,?,'connection_test',NULL,?,'{}',?,'fixture-digest')""",
            (j.actor.admin_id, "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
             json.dumps(config)))
    with pytest.raises(BusinessError) as denied:
        j.repository.claim_next("worker")
    assert denied.value.code == "input_unverified"
    assert "private-secret" not in str(denied.value)
    assert denied.value.__suppress_context__ is True
    assert read_rows("SELECT * FROM job_attempts") == []


@pytest.mark.parametrize("worker", ["", " ", True, "worker\nprivate-secret"])
def test_worker_identity_is_bounded_and_safe(jobs_context, worker):
    j = jobs_context()
    with pytest.raises(BusinessError) as denied:
        j.repository.claim_next(worker)
    assert denied.value.code == "invalid_worker"
    assert "private-secret" not in str(denied.value)
