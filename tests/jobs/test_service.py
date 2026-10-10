"""Submission, ownership, freezing and atomic audit behavior on real SQLite."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
import warnings
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from pydantic import ValidationError

from course_platform.audit import AuditEvent, append_event
from course_platform.database import transaction
from course_platform.domain import Actor, BusinessError
from course_platform.production.models import BriefInput, ReferenceInput


def test_duplicate_submit_returns_original_job_before_stale_revision(jobs_context, read_rows):
    j = jobs_context()
    p = j.project()
    first = j.submit(p)
    claim = j.repository.claim_next("worker-one")
    with transaction(j.db_path, immediate=True) as connection:
        j.repository.finish_in_tx(connection, claim, "succeeded", None)
    j.projects.update(j.actor, p.id, p.revision, BriefInput(title="changed"))
    repeated = j.submit(p)
    assert repeated.id == first.id
    assert repeated.state == "succeeded"
    assert len(read_rows("SELECT * FROM production_jobs")) == 1
    assert len(read_rows("SELECT * FROM token_reservations")) == 1
    assert len(read_rows("SELECT * FROM admin_events WHERE action='job.submit' AND outcome='success'")) == 1


@pytest.mark.parametrize("change", ["project", "kind", "revision", "config"])
def test_same_key_different_digest_rejects(jobs_context, read_rows, change):
    j = jobs_context()
    p = j.project()
    j.submit(p)
    q = j.project() if change == "project" else p
    req = j.request(kind="outline_generate" if change == "kind" else "course_draft_generate",
        project_id=q.id, project_revision=q.revision + (change == "revision"))
    config = j.config.model_copy(update={"model": "other-model"}) if change == "config" else j.config
    with pytest.raises(BusinessError) as denied:
        j.service.submit(j.actor, req, config, idempotency_key="test-key",
                         reserved_tokens=100)
    assert denied.value.code == "idempotency_conflict"
    assert denied.value.status_code == 409
    assert len(read_rows("SELECT * FROM production_jobs")) == 1


def test_two_tabs_cannot_queue_two_modifying_jobs(jobs_context, read_rows):
    j = jobs_context()
    p = j.project()
    barrier = Barrier(2)

    def submit(key):
        barrier.wait(timeout=5)
        try:
            return j.submit(p, key=key)
        except BusinessError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ["tab-one", "tab-two"]))
    assert sum(not isinstance(result, str) for result in results) == 1
    assert "project_busy" in results
    assert len(read_rows("SELECT * FROM production_jobs")) == 1
    assert len(read_rows("SELECT * FROM job_requests")) == 1
    assert len(read_rows("SELECT * FROM token_reservations")) == 1


def test_two_tabs_same_key_get_one_job(jobs_context, read_rows):
    j = jobs_context()
    p = j.project()
    barrier = Barrier(2)

    def submit(_):
        barrier.wait(timeout=5)
        return j.submit(p)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert results[0].id == results[1].id
    assert len(read_rows("SELECT * FROM production_jobs")) == 1


def test_precise_generation_requires_current_confirmation(jobs_context):
    j = jobs_context()
    p = j.project(mode="precise", outline=True)
    with pytest.raises(BusinessError) as denied:
        j.submit(p, kind="chapters_generate")
    assert denied.value.code == "outline_unconfirmed"
    j.drafts.confirm_outline(j.actor, p.id, p.revision)
    p = j.projects.get(p.id)
    assert j.submit(p, kind="chapters_generate").state == "queued"


def test_reference_change_invalidates_precise_generation(jobs_context):
    j = jobs_context()
    p = j.project(mode="precise", outline=True)
    j.drafts.confirm_outline(j.actor, p.id, p.revision)
    p = j.projects.get(p.id)
    j.projects.save_reference(j.actor, p.id, p.revision, None, ReferenceInput(text="private-ref"))
    p = j.projects.get(p.id)
    with pytest.raises(BusinessError) as denied:
        j.submit(p, kind="chapters_generate")
    assert denied.value.code == "outline_unconfirmed"


def test_precise_cannot_use_quick_full_generation(jobs_context):
    j = jobs_context()
    p = j.project(mode="precise")
    with pytest.raises(BusinessError) as denied:
        j.submit(p)
    assert denied.value.code == "invalid_job_mode"


@pytest.mark.parametrize("kind", ["course_draft_generate", "outline_generate", "chapters_generate",
                                  "chapter_regenerate", "draft_validate", "connection_test"])
def test_six_fixed_kinds_are_queued(jobs_context, kind):
    j = jobs_context()
    p = None if kind == "connection_test" else j.project(outline=True)
    ids = (j.drafts.freeze_input(p.id).chapters[0].id,) if kind == "chapter_regenerate" else ()
    assert j.submit(p, kind=kind, chapter_ids=ids).state == "queued"


@pytest.mark.parametrize("field,value", [
    ("kind", "publish"), ("project_id", True), ("project_id", 2**63),
    ("project_revision", False), ("project_revision", 0), ("chapter_ids", ("x", "x")),
    ("parent_job_id", -1), ("extra", "private-secret"),
])
def test_job_request_strict_validation(jobs_context, field, value):
    j = jobs_context()
    data = dict(kind="outline_generate", project_id=1, project_revision=1)
    data[field] = value
    with pytest.raises(ValidationError) as denied:
        j.request(**data)
    assert "private-secret" not in str(denied.value)


@pytest.mark.parametrize("case", ["foreign_chapter", "missing_outline", "archive", "stale"])
def test_submission_object_guards(jobs_context, read_rows, case):
    j = jobs_context()
    p = j.project(outline=case != "missing_outline")
    q = j.project(outline=True)
    kind = "chapter_regenerate" if case in ("foreign_chapter", "missing_outline") else "outline_generate"
    ids = (j.drafts.freeze_input(q.id).chapters[0].id,) if kind == "chapter_regenerate" else ()
    if case == "archive":
        j.projects.archive(j.actor, p.id, p.revision)
        p = j.projects.get(p.id)
    if case == "stale":
        j.projects.update(j.actor, p.id, p.revision, BriefInput(title="new"))
    with pytest.raises(BusinessError) as denied:
        j.submit(p, kind=kind, chapter_ids=ids)
    expected = {"foreign_chapter": "chapter_missing", "missing_outline": "outline_missing",
                "archive": "project_archived", "stale": "stale_revision"}
    assert denied.value.code == expected[case]
    assert read_rows("SELECT * FROM production_jobs") == []


@pytest.mark.parametrize("identity", [None, Actor(True, "test"), Actor(9999, "test")])
def test_submission_requires_current_enabled_owner(jobs_context, identity):
    j = jobs_context()
    p = j.project()
    with pytest.raises(BusinessError) as denied:
        j.service.submit(identity, j.request(kind="outline_generate", project_id=p.id, project_revision=p.revision),
                         j.config, idempotency_key="key", reserved_tokens=100)
    assert denied.value.code == "owner_required"


def test_duplicate_does_not_bypass_disabled_owner(jobs_context):
    j = jobs_context()
    p = j.project()
    j.submit(p)
    with transaction(j.db_path, immediate=True) as connection:
        connection.execute("UPDATE admins SET enabled=0 WHERE id=?", (j.actor.admin_id,))
    with pytest.raises(BusinessError) as denied:
        j.submit(p)
    assert denied.value.code == "owner_required"


@pytest.mark.parametrize("value", [True, -1, "100", 2**63])
def test_reservation_is_strict_server_integer(jobs_context, value):
    j = jobs_context()
    with pytest.raises(BusinessError) as denied:
        j.submit(j.project(), reserved_tokens=value)
    assert denied.value.code == "invalid_reservation"


@pytest.mark.parametrize("key", ["", " ", "x"*129, "key\nsecret"])
def test_invalid_idempotency_keys_are_safely_rejected(jobs_context, key):
    j = jobs_context()
    with pytest.raises(BusinessError) as denied:
        j.submit(j.project(), key=key)
    assert denied.value.code == "invalid_idempotency_key"
    assert "secret" not in str(denied.value)


def test_comprehensive_freeze_matches_immutable_sources(jobs_context, read_rows):
    j = jobs_context()
    p = j.project(outline=True)
    j.projects.save_reference(j.actor, p.id, p.revision, None,
        ReferenceInput(text="private-ref-e\u0301\r\n", source_note="private-source", source_status="owner_noted"))
    p = j.projects.get(p.id)
    f = j.drafts.freeze_input(p.id)
    c = f.chapters[0]
    j.drafts.save_chapter(j.actor, p.id, c.id, None, "private-body-😀")
    p = j.projects.get(p.id)
    j.submit(p, kind="outline_generate")
    row = read_rows("SELECT * FROM production_jobs")[0]
    raw = json.loads(row["input_json"])
    assert set(raw) == {"project_id","project_revision","mode","brief_revision","brief",
                        "references","outline","chapters","input_hash"}
    assert set(raw["brief"]) == {"title","audience","prerequisites","goal","difficulty",
                                 "chapter_count","category_id","content_kind"}
    assert raw["chapters"][0] == {"id": c.id, "body_revision": 1, "markdown": "private-body-😀"}
    assert raw["references"][0]["data"]["text"] == "private-ref-e\u0301\r\n"
    assert set(raw["outline"]) == {"id","revision","project_id","items","confirmed"}
    assert raw["project_revision"] == row["project_revision"] == p.revision
    assert raw["input_hash"] == row["input_hash"]
    source = {key: value for key, value in raw.items() if key not in ("project_revision", "input_hash")}
    source["outline"] = {key: value for key,value in source["outline"].items() if key != "confirmed"}
    canonical = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",",":"), allow_nan=False)
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == row["input_hash"]
    assert row["input_json"] == json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",",":"), allow_nan=False)
    assert set(json.loads(row["config_json"])) == {"provider","endpoint_fingerprint","model","context_tokens",
        "prompt_version","outline_output_tokens","chapter_output_tokens"}
    assert "private-" not in row["config_json"]
    assert "private-" not in str(j.service.get(row["id"]))
    events = read_rows("SELECT * FROM admin_events WHERE action LIKE 'job.%'")
    assert "private-" not in str(events)
    assert events[0]["changes_json"] == '{"job_state":"queued","revision":1}'


def test_job_reads_never_maintain_or_write(jobs_context, read_rows):
    j = jobs_context()
    job = j.submit(j.project())
    j.repository.claim_next("worker")
    j.clock.advance(seconds=200)
    before = j.db_path.read_bytes()
    assert j.service.get(job.id).state == "running"
    assert j.service.list_jobs() == ([j.service.get(job.id)], 1)
    assert j.db_path.read_bytes() == before
    assert len(read_rows("SELECT * FROM admin_events WHERE action='job.interrupt'")) == 0


def test_job_list_pages_are_bounded_and_ordered(jobs_context):
    j = jobs_context()
    ids = [j.submit(kind="connection_test", key=f"key-{n}").id for n in range(21)]
    first, count = j.service.list_jobs()
    second, count2 = j.service.list_jobs(page=2)
    assert [x.id for x in first] == list(reversed(ids[1:]))
    assert [x.id for x in second] == ids[:1]
    assert count == count2 == 21


@pytest.mark.parametrize("change", [{"api_key":"private-secret"}, {"endpoint_fingerprint":"https://key@example.test"},
    {"context_tokens":True}, {"outline_output_tokens":4001}, {"chapter_output_tokens":0}])
def test_model_public_rejects_secrets_and_invalid_limits(jobs_context, change):
    j = jobs_context()
    with pytest.raises(ValidationError) as denied:
        type(j.config)(**(j.config.model_dump() | change))
    assert "private-secret" not in str(denied.value)
    assert "key@example" not in str(denied.value)


def test_defensive_model_validation_has_no_private_warnings(jobs_context, capsys, caplog, read_rows):
    j = jobs_context()
    p = j.project()
    bad = type(j.config).model_construct(**(j.config.model_dump() | {"context_tokens":"private-secret"}))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(BusinessError) as denied:
            j.service.submit(j.actor, j.request(kind="outline_generate", project_id=p.id, project_revision=p.revision),
                bad, idempotency_key="key", reserved_tokens=100)
    assert denied.value.code == "invalid_job"
    assert "private-secret" not in str(denied.value)
    assert caught == []
    assert "private-secret" not in capsys.readouterr().out + caplog.text + str(read_rows("SELECT * FROM admin_events"))


def test_submit_audit_failure_rolls_back_all_job_rows(jobs_context, read_rows, monkeypatch):
    j = jobs_context()
    p = j.project()

    def fail(*args):
        raise BusinessError("audit_unavailable", "safe", 503)
    monkeypatch.setattr("course_platform.jobs.service.append_event", fail)
    with pytest.raises(BusinessError):
        j.submit(p)
    assert read_rows("SELECT * FROM production_jobs") == []
    assert read_rows("SELECT * FROM token_reservations") == []
    assert read_rows("SELECT * FROM job_requests") == []


@pytest.mark.parametrize("state", ["queued", "running", "succeeded", "failed", "cancelled", "interrupted"])
def test_job_state_has_separate_strict_audit_enum(jobs_context, state):
    j = jobs_context()
    with transaction(j.db_path, immediate=True) as connection:
        append_event(connection, AuditEvent(j.actor.admin_id, "job", "1", "job.submit",
            "completed", "success", "test", {"job_state": state}))


@pytest.mark.parametrize("changes,action", [
    ({"job_state":"active"}, "job.submit"), ({"job_state":True}, "job.submit"),
    ({"status":"running"}, "product.create"), ({"status":"failed"}, "order.create"),
    ({"status":"running"}, "job.submit"), ({"input_json":"private-secret"}, "job.submit"),
])
def test_job_audit_does_not_broaden_product_order_or_payloads(jobs_context, changes, action):
    j = jobs_context()
    with pytest.raises(BusinessError) as denied:
        with transaction(j.db_path, immediate=True) as connection:
            append_event(connection, AuditEvent(j.actor.admin_id, "job", "1", action,
                "completed", "success", "test", changes))
    assert denied.value.code == "audit_payload"


@pytest.mark.parametrize("page", [True, 0, -1, 2**63, 2**62])
def test_invalid_job_pages_are_safe_business_errors(jobs_context, page):
    j = jobs_context()
    with pytest.raises(BusinessError) as denied:
        j.service.list_jobs(page=page)
    assert denied.value.code == "invalid_identifier"


def test_parent_job_must_be_terminal_and_same_project(jobs_context):
    j = jobs_context()
    p, q = j.project(), j.project()
    original = j.submit(p)
    for target in (p,q):
        with pytest.raises(BusinessError):
            j.service.submit(j.actor, j.request(kind="draft_validate", project_id=target.id,
                project_revision=target.revision, parent_job_id=original.id), j.config,
                idempotency_key=f"parent-{target.id}", reserved_tokens=100)
    claim = j.repository.claim_next("worker")
    with transaction(j.db_path, immediate=True) as connection:
        j.repository.finish_in_tx(connection, claim, "failed", "model_invalid")
    result = j.service.submit(j.actor, j.request(kind="outline_generate", project_id=p.id,
        project_revision=p.revision, parent_job_id=original.id), j.config,
        idempotency_key="explicit-new-job", reserved_tokens=100)
    assert result.id != original.id
    assert j.service.get(original.id).state == "failed"


def test_chapter_selection_digest_is_order_independent(jobs_context):
    j = jobs_context()
    p = j.project(outline=True)
    ids = tuple(c.id for c in j.drafts.freeze_input(p.id).chapters)
    first = j.submit(p, kind="chapters_generate", chapter_ids=ids)
    again = j.submit(p, kind="chapters_generate", chapter_ids=tuple(reversed(ids)))
    assert first.id == again.id


def test_job_imports_have_no_runtime_side_effects(jobs_context):
    j = jobs_context()
    script = """
import builtins, os, sqlite3, socket, threading
original_open = builtins.open
def safe_open(file, mode="r", *args, **kwargs):
    if any(flag in mode for flag in "wax+") or str(file).endswith(".env"):
        raise AssertionError("private file side effect")
    return original_open(file, mode, *args, **kwargs)
builtins.open = safe_open
def deny(*args, **kwargs):
    raise AssertionError('runtime side effect')
sqlite3.connect = deny
os.chdir = deny
socket.create_connection = deny
socket.socket = deny
threading.Thread.start = deny
import course_platform.jobs
import course_platform.jobs.models
import course_platform.jobs.repository
import course_platform.jobs.service
print('import-only')
"""
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", script], cwd=j.db_path.parent,
        env={**os.environ, "PYTHONPATH":str(Path(__file__).resolve().parents[2]), "PYTHONDONTWRITEBYTECODE":"1"},
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "import-only"

def test_constructor_bypassed_missing_request_fields_fail_safely(jobs_context, read_rows):
    j = jobs_context()
    malformed = j.request.model_construct(kind="outline_generate", project_revision=1)
    with pytest.raises(BusinessError) as denied:
        j.service.submit(j.actor, malformed, j.config, idempotency_key="key", reserved_tokens=100)
    assert denied.value.code == "invalid_job"
    assert read_rows("SELECT * FROM production_jobs") == []

@pytest.mark.parametrize("field", ["provider", "model", "prompt_version"])
@pytest.mark.parametrize("name", ["task-model", "AICS-model", "BULK-v1"])
def test_public_identifiers_accept_incidental_credential_substrings(jobs_context, read_rows, field, name):
    j = jobs_context()
    config = type(j.config)(**(j.config.model_dump() | {field: name}))
    # Public identifier syntax is distinct from the adapter's supported profile.
    # Submission drift/unsupported execution behavior is covered separately.
    assert config.model_dump()[field] == name


@pytest.mark.parametrize("field", ["provider", "model", "prompt_version"])
@pytest.mark.parametrize("name", [
    "sk-private-secret", "CS-private-secret", "LK-private-secret",
    "vendor/sk-private-secret", "namespace:CS-private-secret", "safe.LK-private-secret",
    "https://owner:private-secret@example.test/model",
])
def test_public_identifiers_still_reject_credential_forms(jobs_context, field, name):
    j = jobs_context()
    with pytest.raises(ValidationError) as denied:
        type(j.config)(**(j.config.model_dump() | {field: name}))
    assert "private-secret" not in str(denied.value)
    assert "owner:" not in str(denied.value)
