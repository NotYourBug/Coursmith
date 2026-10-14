"""Real SQLite contracts for private projects and append-only input revisions."""

import importlib
import json
import sqlite3
import warnings
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from course_platform.audit import AuditEvent, append_event
from course_platform.database import transaction
from course_platform.domain import Actor, BusinessError


def components(db_path, clock):
    # Import in the test body so missing Task2 code is a feature failure,
    # rather than a fixture/collection error in the initial RED run.
    models = importlib.import_module("course_platform.production.models")
    projects = importlib.import_module("course_platform.production.projects")
    return projects.ProjectService(db_path, clock=clock.now), models.BriefInput, models.ReferenceInput


def read(db_path, sql, values=()):
    with transaction(db_path) as conn:
        return [dict(row) for row in conn.execute(sql, values)]


def seed_confirmation(db_path, project, owner):
    """Build complete schema6 outline binding without future DraftService APIs."""
    with transaction(db_path, immediate=True) as conn:
        conn.execute("INSERT INTO production_chapters (id,project_id) VALUES ('chapter-test',?)", (project.id,))
        outline = conn.execute(
            """INSERT INTO outline_revisions
               (project_id,revision,item_count,input_hash,source,actor_admin_id)
               VALUES (?,1,1,'frozen-input','manual',?)""", (project.id, owner.admin_id)).lastrowid
        conn.execute("""INSERT INTO outline_items
                     (outline_revision_id,project_id,position,chapter_id,title,goal)
                     VALUES (?,?,1,'chapter-test','章','目标')""", (outline, project.id))
        confirmation = conn.execute("""INSERT INTO outline_confirmations
                         (project_id,outline_revision_id,input_hash,actor_admin_id)
                         VALUES (?,?,'confirmed-input',?)""", (project.id, outline, owner.admin_id)).lastrowid
        conn.execute("""UPDATE production_projects SET current_outline_revision_id=?,
                       current_confirmation_id=?,acknowledged_input_hash='acknowledged' WHERE id=?""",
                     (outline, confirmation, project.id))
    return confirmation


def seed_job(db_path, project, owner, state, kind="outline_generate"):
    with transaction(db_path, immediate=True) as conn:
        conn.execute("""INSERT INTO production_jobs
            (project_id,actor_admin_id,kind,project_revision,state,input_hash,input_json,config_json,request_digest)
            VALUES (?,?,?,?,?,'input','{}','{}','digest')""",
            (project.id, owner.admin_id, kind, project.revision, state))


def test_title_only_quick_project_uses_defaults(db_path, clock, product_owner):
    service, BriefInput, _ = components(db_path, clock)
    project = service.create(product_owner, BriefInput(title="Agent开发"), mode="quick")
    assert (project.revision, project.mode, project.status, project.brief_revision) == (1, "quick", "draft", 1)
    assert project.brief.chapter_count == 10
    assert project.brief.audience == project.brief.goal == project.brief.prerequisites == project.brief.difficulty == ""
    assert project.brief.category_id is None and project.brief.content_kind == "course"
    assert service.get(project.id) == project
    assert read(db_path, "SELECT * FROM outline_confirmations") == []
    row = read(db_path, "SELECT * FROM production_projects")[0]
    assert row["current_confirmation_id"] is None
    assert row["created_at"] == row["updated_at"] == "2026-10-02T00:00:00+00:00"
    assert read(db_path, "SELECT * FROM production_jobs") == []


@pytest.mark.parametrize("count", [1, 60])
def test_chapter_count_boundaries_are_saved(db_path, clock, product_owner, count):
    service, BriefInput, _ = components(db_path, clock)
    assert service.create(product_owner, BriefInput(title="资料", chapter_count=count), mode="precise").brief.chapter_count == count


@pytest.mark.parametrize("fields", [
    {"chapter_count": 0}, {"chapter_count": 61}, {"chapter_count": True},
    {"chapter_count": "10"}, {"chapter_count": 10.0}, {"title": " \t\n\u3000"},
    {"title": "x" * 201}, {"title": 123}, {"title": "a\x00b"},
    {"audience": "x" * 2001}, {"goal": "x" * 2001},
    {"prerequisites": "x" * 2001}, {"difficulty": "x" * 2001},
    {"category_id": True}, {"category_id": 0}, {"extra_field": "not allowed"},
    {"content_kind": "unknown"},
])
def test_strict_brief_rejects_invalid_fields(db_path, clock, fields):
    _, BriefInput, _ = components(db_path, clock)
    with pytest.raises(ValidationError):
        BriefInput(**({"title": "资料"} | fields))


@pytest.mark.parametrize("fields", [
    {"text": "x" * 20001}, {"text": 123}, {"source_note": "x" * 1001},
    {"source_status": "verified"}, {"extra_field": "no"}, {"text": "a\x00b"},
])
def test_strict_reference_rejects_invalid_fields(db_path, clock, fields):
    _, _, ReferenceInput = components(db_path, clock)
    with pytest.raises(ValidationError):
        ReferenceInput(**({"text": "资料"} | fields))


@pytest.mark.parametrize("kind", ["guide", "handbook"])
def test_only_course_is_available(db_path, clock, product_owner, kind):
    service, BriefInput, _ = components(db_path, clock)
    with pytest.raises(BusinessError) as denied:
        service.create(product_owner, BriefInput(title="资料", content_kind=kind), mode="quick")
    assert denied.value.code == "unsupported_content_kind"
    assert read(db_path, "SELECT * FROM production_projects") == []


def test_reference_aggregate_limit_includes_current_active_revisions_only(db_path, clock, product_owner):
    service, BriefInput, ReferenceInput = components(db_path, clock)
    p = service.create(product_owner, BriefInput(title="资料"), mode="quick")
    a = service.save_reference(product_owner, p.id, 1, None, ReferenceInput(text="a" * 10000))
    b = service.save_reference(product_owner, p.id, 2, None, ReferenceInput(text="b" * 10000))
    assert service.get(p.id).revision == 3
    with pytest.raises(BusinessError) as denied:
        service.save_reference(product_owner, p.id, 3, b.id, ReferenceInput(text="b" * 10001))
    assert denied.value.code == "reference_limit"
    assert service.get(p.id).revision == 3
    smaller = service.save_reference(product_owner, p.id, 3, a.id, ReferenceInput(text="small"))
    assert smaller.revision == 2 and smaller.id == a.id
    assert service.remove_reference(product_owner, p.id, 4, b.id).revision == 5
    assert service.save_reference(product_owner, p.id, 5, None, ReferenceInput(text="c" * 19995)).revision == 1
    assert len(read(db_path, "SELECT * FROM reference_revisions")) == 4


def test_eleventh_active_reference_is_rejected_and_removed_id_not_reused(db_path, clock, product_owner):
    service, BriefInput, ReferenceInput = components(db_path, clock)
    p = service.create(product_owner, BriefInput(title="资料"), mode="quick")
    refs = [service.save_reference(product_owner, p.id, i + 1, None, ReferenceInput(text="")) for i in range(10)]
    with pytest.raises(BusinessError) as denied:
        service.save_reference(product_owner, p.id, 11, None, ReferenceInput(text=""))
    assert denied.value.code == "reference_limit"
    service.remove_reference(product_owner, p.id, 11, refs[0].id)
    new = service.save_reference(product_owner, p.id, 12, None, ReferenceInput(text="new"))
    assert new.id not in {r.id for r in refs}
    with pytest.raises(BusinessError):
        service.save_reference(product_owner, p.id, 13, refs[0].id, ReferenceInput(text="resurrect"))
    with pytest.raises(BusinessError):
        service.remove_reference(product_owner, p.id, 13, refs[0].id)
    assert read(db_path, "SELECT active FROM project_reference_links WHERE reference_id=?", (refs[0].id,)) == [{"active": 0}]


def test_emoji_is_counted_as_unicode_but_markdown_size_as_utf8(db_path, clock, product_owner):
    service, BriefInput, ReferenceInput = components(db_path, clock)
    p = service.create(product_owner, BriefInput(title="😀" * 200, goal="中" * 2000), mode="quick")
    ref = service.save_reference(product_owner, p.id, 1, None, ReferenceInput(text="😀" * 20000, source_note="中" * 1000))
    assert len(ref.data.text) == 20000 and len(ref.data.text.encode("utf-8")) == 80000
    markdown = "😀" * 51200
    with transaction(db_path, immediate=True) as conn:
        conn.execute("INSERT INTO production_chapters (id,project_id) VALUES ('unicode-chapter',?)", (p.id,))
        conn.execute("""INSERT INTO chapter_revisions (project_id,chapter_id,revision,markdown,source,actor_admin_id)
                     VALUES (?,'unicode-chapter',1,?,'manual',?)""", (p.id, markdown, product_owner.admin_id))
    assert read(db_path, "SELECT length(CAST(markdown AS BLOB)) AS size FROM chapter_revisions") == [{"size": 204800}]
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(db_path, immediate=True) as conn:
            conn.execute("""INSERT INTO chapter_revisions (project_id,chapter_id,revision,markdown,source,actor_admin_id)
                         VALUES (?,'unicode-chapter',2,?,'manual',?)""", (p.id, markdown + "x", product_owner.admin_id))


def test_reference_change_keeps_history_and_invalidates_confirmation(db_path, clock, product_owner):
    service, BriefInput, ReferenceInput = components(db_path, clock)
    p = service.create(product_owner, BriefInput(title="资料", chapter_count=1), mode="precise")
    a = service.save_reference(product_owner, p.id, 1, None, ReferenceInput(text="old", source_note="note", source_status="owner_noted"))
    confirmation = seed_confirmation(db_path, p, product_owner)
    old = read(db_path, "SELECT * FROM reference_revisions")
    new = service.save_reference(product_owner, p.id, 2, a.id,
                                 ReferenceInput(text="new", source_note="awaiting source check", source_status="pending"))
    assert (new.id, new.revision, new.data.text) == (a.id, 2, "new")
    assert read(db_path, "SELECT * FROM reference_revisions ORDER BY id")[0] == old[0]
    assert read(db_path, "SELECT id FROM outline_confirmations") == [{"id": confirmation}]
    row = read(db_path, "SELECT * FROM production_projects")[0]
    assert row["current_confirmation_id"] is None and row["acknowledged_input_hash"] is None
    assert read(db_path, "SELECT needs_review,review_reason FROM production_chapters") == [{"needs_review": 1, "review_reason": "input_changed"}]
    assert row["current_outline_revision_id"] is not None
    service.remove_reference(product_owner, p.id, 3, a.id)
    assert len(read(db_path, "SELECT * FROM reference_revisions")) == 2
    assert read(db_path, "SELECT active,current_revision_id FROM project_reference_links")[0] == {"active": 0, "current_revision_id": old[0]["id"] + 1}


@pytest.mark.parametrize("operation", ["update", "mode", "reference_add", "reference_remove"])
def test_each_input_change_invalidates_confirmation_without_erasing_sources(db_path, clock, product_owner, operation):
    service, BriefInput, ReferenceInput = components(db_path, clock)
    p = service.create(product_owner, BriefInput(title="old", chapter_count=1), mode="precise")
    ref = service.save_reference(product_owner, p.id, 1, None, ReferenceInput(text="old"))
    confirmation = seed_confirmation(db_path, p, product_owner)
    if operation == "update":
        result = service.update(product_owner, p.id, 2, BriefInput(title="new", chapter_count=1))
        assert result.brief_revision == 2
        assert read(db_path, "SELECT title FROM brief_revisions ORDER BY revision") == [{"title": "old"}, {"title": "new"}]
    elif operation == "mode":
        assert service.set_mode(product_owner, p.id, 2, "quick").mode == "quick"
    elif operation == "reference_add":
        service.save_reference(product_owner, p.id, 2, None, ReferenceInput(text="new"))
    else:
        service.remove_reference(product_owner, p.id, 2, ref.id)
    row = read(db_path, "SELECT * FROM production_projects")[0]
    assert row["revision"] == 3 and row["current_confirmation_id"] is None
    assert read(db_path, "SELECT id FROM outline_confirmations") == [{"id": confirmation}]


@pytest.mark.parametrize("actor_kind", ["none", "dict", "duck", "missing", "disabled", "bool", "string"])
def test_untrusted_actor_rejected_without_leaking_or_mutating(db_path, clock, product_owner, actor_kind):
    service, BriefInput, _ = components(db_path, clock)
    actors = {"none": None, "dict": {"admin_id": 1, "request_id": "fake"},
              "duck": SimpleNamespace(admin_id=1, request_id="fake"),
              "missing": Actor(999, "fake"), "disabled": product_owner,
              "bool": Actor(True, "fake"), "string": Actor("1", "fake")}
    if actor_kind == "disabled":
        with transaction(db_path, immediate=True) as conn:
            conn.execute("UPDATE admins SET enabled=0 WHERE id=1")
    with pytest.raises(BusinessError) as denied:
        service.create(actors[actor_kind], BriefInput(title="private"), mode="quick")
    assert denied.value.code == "owner_required" and denied.value.status_code == 403
    assert read(db_path, "SELECT * FROM production_projects") == []


def test_untrusted_actor_and_cross_project_reference_rejected(db_path, clock, product_owner):
    service, BriefInput, ReferenceInput = components(db_path, clock)
    a = service.create(product_owner, BriefInput(title="A"), mode="quick")
    b = service.create(product_owner, BriefInput(title="B"), mode="precise")
    ref = service.save_reference(product_owner, a.id, 1, None, ReferenceInput(text="A secret"))
    other = service.save_reference(product_owner, b.id, 1, None, ReferenceInput(text="B secret"))
    assert ref.id != other.id
    before = read(db_path, "SELECT * FROM reference_revisions")
    with pytest.raises(BusinessError) as denied:
        service.save_reference(product_owner, b.id, 2, ref.id, ReferenceInput(text="wrong project"))
    assert denied.value.code == "reference_missing"
    with pytest.raises(BusinessError):
        service.remove_reference(product_owner, b.id, 2, ref.id)
    assert read(db_path, "SELECT * FROM reference_revisions") == before
    assert service.get(b.id).revision == 2


@pytest.mark.parametrize("state", ["queued", "running"])
@pytest.mark.parametrize("operation", ["update", "mode", "archive", "save_reference", "remove_reference"])
def test_active_modifying_job_blocks_project_edits(db_path, clock, product_owner, state, operation):
    service, BriefInput, ReferenceInput = components(db_path, clock)
    p = service.create(product_owner, BriefInput(title="old"), mode="quick")
    ref = service.save_reference(product_owner, p.id, 1, None, ReferenceInput(text="old"))
    p = service.get(p.id)
    seed_job(db_path, p, product_owner, state)
    actions = {"update": lambda: service.update(product_owner, p.id, 2, BriefInput(title="new")),
               "mode": lambda: service.set_mode(product_owner, p.id, 2, "precise"),
               "archive": lambda: service.archive(product_owner, p.id, 2),
               "save_reference": lambda: service.save_reference(product_owner, p.id, 2, ref.id, ReferenceInput(text="new")),
               "remove_reference": lambda: service.remove_reference(product_owner, p.id, 2, ref.id)}
    with pytest.raises(BusinessError) as denied:
        actions[operation]()
    assert denied.value.code == "project_busy"
    assert service.get(p.id) == p


def test_terminal_job_releases_edit_guard(db_path, clock, product_owner):
    service, BriefInput, _ = components(db_path, clock)
    p = service.create(product_owner, BriefInput(title="old"), mode="quick")
    seed_job(db_path, p, product_owner, "interrupted")
    assert service.update(product_owner, p.id, 1, BriefInput(title="new")).brief.title == "new"


@pytest.mark.parametrize("operation", ["update", "mode", "archive", "save_reference", "remove_reference"])
def test_archived_project_keeps_history_and_blocks_edits(db_path, clock, product_owner, operation):
    service, BriefInput, ReferenceInput = components(db_path, clock)
    p = service.create(product_owner, BriefInput(title="old"), mode="quick")
    ref = service.save_reference(product_owner, p.id, 1, None, ReferenceInput(text="old"))
    archived = service.archive(product_owner, p.id, 2)
    assert archived.status == "archived" and archived.revision == 3
    actions = {"update": lambda: service.update(product_owner, p.id, 3, BriefInput(title="new")),
               "mode": lambda: service.set_mode(product_owner, p.id, 3, "precise"),
               "archive": lambda: service.archive(product_owner, p.id, 3),
               "save_reference": lambda: service.save_reference(product_owner, p.id, 3, ref.id, ReferenceInput(text="new")),
               "remove_reference": lambda: service.remove_reference(product_owner, p.id, 3, ref.id)}
    with pytest.raises(BusinessError) as denied:
        actions[operation]()
    assert denied.value.code == "project_archived"
    assert service.get(p.id) == archived
    assert len(read(db_path, "SELECT * FROM reference_revisions")) == 1


@pytest.mark.parametrize("revision", [0, 2, True, "1"])
def test_stale_or_noninteger_revision_never_overwrites_brief(db_path, clock, product_owner, revision):
    service, BriefInput, _ = components(db_path, clock)
    p = service.create(product_owner, BriefInput(title="old"), mode="quick")
    with pytest.raises(BusinessError) as denied:
        service.update(product_owner, p.id, revision, BriefInput(title="new"))
    assert denied.value.code == "stale_revision"
    assert service.get(p.id) == p
    assert len(read(db_path, "SELECT * FROM brief_revisions")) == 1


def test_list_paginates_twenty_and_reads_do_not_write(db_path, clock, product_owner):
    service, BriefInput, _ = components(db_path, clock)
    projects = [service.create(product_owner, BriefInput(title=str(i)), mode="quick") for i in range(22)]
    service.archive(product_owner, projects[0].id, 1)
    before = read(db_path, "SELECT * FROM admin_events")
    first, total = service.list_projects()
    second, total2 = service.list_projects(page=2)
    archived, archived_total = service.list_projects(archived=True)
    assert len(first) == 20 and len(second) == 1 and total == total2 == 21
    assert [p.id for p in first + second] == [p.id for p in reversed(projects[1:])]
    assert [p.id for p in archived] == [projects[0].id] and archived_total == 1
    assert service.list_projects(page=3) == ([], 21)
    assert read(db_path, "SELECT * FROM admin_events") == before
    for page in (0, True, "1"):
        with pytest.raises(BusinessError):
            service.list_projects(page=page)
    with pytest.raises(BusinessError):
        service.get(999)


def test_category_validation_and_model_construct_cannot_bypass_strict_input(db_path, clock, product_owner):
    service, BriefInput, _ = components(db_path, clock)
    with pytest.raises(BusinessError) as denied:
        service.create(product_owner, BriefInput(title="private", category_id=999), mode="quick")
    assert denied.value.code == "category_missing"
    with transaction(db_path, immediate=True) as conn:
        category = conn.execute("INSERT INTO categories (slug,name) VALUES ('test','Category')").lastrowid
    p = service.create(product_owner, BriefInput(title="private", category_id=category), mode="quick")
    assert p.brief.category_id == category
    with pytest.raises(BusinessError) as denied:
        service.update(product_owner, p.id, 1, BriefInput.model_construct(title="private", chapter_count=True))
    assert denied.value.code == "invalid_production"
    assert service.get(p.id).revision == 1


@pytest.mark.parametrize("mode", ["unknown", True, None])
def test_invalid_mode_cannot_create_or_change_project(db_path, clock, product_owner, mode):
    service, BriefInput, _ = components(db_path, clock)
    with pytest.raises(BusinessError) as denied:
        service.create(product_owner, BriefInput(title="private"), mode=mode)
    assert denied.value.code == "invalid_mode"
    p = service.create(product_owner, BriefInput(title="private"), mode="quick")
    with pytest.raises(BusinessError):
        service.set_mode(product_owner, p.id, 1, mode)
    assert service.get(p.id) == p


def test_project_event_never_contains_input_text(db_path, clock, product_owner, caplog):
    service, BriefInput, ReferenceInput = components(db_path, clock)
    secrets = ["PRIVATE_TITLE", "PRIVATE_AUDIENCE", "PRIVATE_GOAL", "PRIVATE_REF", "PRIVATE_NOTE"]
    p = service.create(product_owner, BriefInput(title=secrets[0], audience=secrets[1], goal=secrets[2]), mode="precise")
    service.save_reference(product_owner, p.id, 1, None, ReferenceInput(text=secrets[3], source_note=secrets[4]))
    with pytest.raises(BusinessError) as denied:
        service.update(product_owner, p.id, 1, BriefInput(title=secrets[0]))
    event_module = importlib.import_module("course_platform.production.events")
    event_module.record_production_denial(db_path, product_owner, "production.update", str(p.id), denied.value, request_id="route-request")
    events = read(db_path, "SELECT * FROM admin_events")
    assert len(events) == 4  # owner initialization, project, reference, one deduplicated denial
    audit_text = json.dumps(events) + caplog.text + str(denied.value)
    assert all(secret not in audit_text for secret in secrets)
    for event in events[1:]:
        assert json.loads(event["changes_json"]).keys() <= {"revision", "error_code"}


def test_success_audit_failure_rolls_back_all_source_and_pointer_writes(db_path, clock, product_owner, monkeypatch):
    service, BriefInput, _ = components(db_path, clock)
    def unavailable(*args):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr("course_platform.production.projects.append_event", unavailable)
    with pytest.raises(RuntimeError):
        service.create(product_owner, BriefInput(title="private"), mode="quick")
    assert read(db_path, "SELECT * FROM production_projects") == []
    assert read(db_path, "SELECT * FROM brief_revisions") == []


@pytest.mark.parametrize("action", ["production.create", "production.update", "production.reference", "production.mode", "production.archive"])
def test_production_audit_actions_reject_private_metadata(db_path, clock, action):
    components(db_path, clock)
    with pytest.raises(BusinessError):
        with transaction(db_path, immediate=True) as conn:
            append_event(conn, AuditEvent(None, "production", "1", action, "completed", "success", "audit-test", {"title": "private"}))


def test_validation_error_strings_never_echo_private_input(db_path, clock):
    _, BriefInput, ReferenceInput = components(db_path, clock)
    for model, fields in ((BriefInput, {"title": "PRIVATE_REQUIREMENT" * 20}),
                          (ReferenceInput, {"text": "ref", "source_note": "PRIVATE_SOURCE" * 100})):
        with pytest.raises(ValidationError) as denied:
            model(**fields)
        assert "PRIVATE_REQUIREMENT" not in str(denied.value)
        assert "PRIVATE_SOURCE" not in str(denied.value)


def test_out_of_sqlite_range_identifiers_are_safe_business_errors(db_path, clock, product_owner):
    service, BriefInput, _ = components(db_path, clock)
    p = service.create(product_owner, BriefInput(title="private"), mode="quick")
    for action in (lambda: service.get(2**63),
                   lambda: service.list_projects(page=2**63),
                   lambda: service.create(Actor(2**63, "invalid-owner"), BriefInput(title="private"), mode="quick")):
        with pytest.raises(BusinessError):
            action()
    with pytest.raises(ValidationError):
        BriefInput(title="private", category_id=2**63)
    assert service.get(p.id) == p


def test_two_concurrent_brief_edits_keep_exactly_one_winner(db_path, clock, product_owner):
    service, BriefInput, _ = components(db_path, clock)
    p = service.create(product_owner, BriefInput(title="original"), mode="quick")
    barrier = Barrier(2)

    def edit(title):
        barrier.wait(timeout=5)
        try:
            return service.update(product_owner, p.id, 1, BriefInput(title=title)).brief.title
        except BusinessError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(edit, title) for title in ("first", "second")]
        results = [future.result(timeout=10) for future in futures]
    assert results.count("stale_revision") == 1
    winner = next(value for value in results if value != "stale_revision")
    assert service.get(p.id).brief.title == winner
    assert read(db_path, "SELECT title FROM brief_revisions ORDER BY revision") == [{"title": "original"}, {"title": winner}]


def test_source_metadata_edit_creates_new_hash_and_invalidates_confirmation(db_path, clock, product_owner):
    service, BriefInput, ReferenceInput = components(db_path, clock)
    p = service.create(product_owner, BriefInput(title="private", chapter_count=1), mode="precise")
    ref = service.save_reference(product_owner, p.id, 1, None, ReferenceInput(text="same text"))
    seed_confirmation(db_path, p, product_owner)
    before = read(db_path, "SELECT * FROM reference_revisions")[0]
    edited = service.save_reference(product_owner, p.id, 2, ref.id,
                                    ReferenceInput(text="same text", source_note="owner comment", source_status="owner_noted"))
    rows = read(db_path, "SELECT * FROM reference_revisions ORDER BY id")
    assert rows[0] == before and rows[1]["text"] == before["text"]
    assert rows[1]["content_hash"] != before["content_hash"] and edited.revision == 2
    assert read(db_path, "SELECT current_confirmation_id FROM production_projects") == [{"current_confirmation_id": None}]


def production_inputs(db_path):
    return {table: read(db_path, f"SELECT * FROM {table} ORDER BY 1,2") for table in (
        "production_projects", "brief_revisions", "project_reference_links", "reference_revisions")}


@pytest.mark.parametrize("operation", ["create", "update", "reference_create", "reference_replace"])
def test_defensive_revalidation_never_warns_private_inputs(db_path, clock, product_owner, caplog, capsys, operation):
    service, BriefInput, ReferenceInput = components(db_path, clock)
    p = service.create(product_owner, BriefInput(title="valid"), mode="quick")
    ref = service.save_reference(product_owner, p.id, 1, None, ReferenceInput(text="valid"))
    before = production_inputs(db_path)
    malformed_brief = BriefInput.model_construct(title="valid", goal=["PRIVATE_SENTINEL"])
    malformed_reference = ReferenceInput.model_construct(text=["PRIVATE_SENTINEL"])
    actions = {
        "create": lambda: service.create(product_owner, malformed_brief, mode="quick"),
        "update": lambda: service.update(product_owner, p.id, 2, malformed_brief),
        "reference_create": lambda: service.save_reference(product_owner, p.id, 2, None, malformed_reference),
        "reference_replace": lambda: service.save_reference(product_owner, p.id, 2, ref.id, malformed_reference),
    }
    with warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter("always")
        with pytest.raises(BusinessError) as denied:
            actions[operation]()
    assert denied.value.code == "invalid_production" and denied.value.status_code == 400
    assert production_inputs(db_path) == before
    assert observed == []
    output = capsys.readouterr()
    assert "PRIVATE_SENTINEL" not in output.out + output.err + caplog.text + str(denied.value)
    assert "PRIVATE_SENTINEL" not in json.dumps(read(db_path, "SELECT * FROM admin_events"))


@pytest.mark.parametrize("status", ["pending", "owner_noted"])
@pytest.mark.parametrize("note", ["", " \t\n\u3000"])
def test_source_status_without_explanation_is_rejected_by_input(db_path, clock, status, note):
    _, _, ReferenceInput = components(db_path, clock)
    with pytest.raises(ValidationError):
        ReferenceInput(text="private body", source_note=note, source_status=status)


@pytest.mark.parametrize("operation", ["create", "replace"])
@pytest.mark.parametrize("status", ["pending", "owner_noted"])
@pytest.mark.parametrize("note", ["", " \t\n\u3000"])
def test_source_status_without_explanation_cannot_be_saved(db_path, clock, product_owner, operation, status, note):
    service, BriefInput, ReferenceInput = components(db_path, clock)
    p = service.create(product_owner, BriefInput(title="valid"), mode="quick")
    ref = service.save_reference(product_owner, p.id, 1, None, ReferenceInput(text="original body"))
    before = production_inputs(db_path)
    data = ReferenceInput.model_construct(text="new body", source_note=note, source_status=status)
    with pytest.raises(BusinessError) as denied:
        service.save_reference(product_owner, p.id, 2, ref.id if operation == "replace" else None, data)
    assert denied.value.code == "invalid_production" and denied.value.status_code == 400
    assert production_inputs(db_path) == before


@pytest.mark.parametrize("status,note", [
    ("unprovided", ""), ("unprovided", " \t\n\u3000"),
    ("pending", " \nuncorroborated source\u3000 "),
    ("owner_noted", " \nowner explanation\u3000 "),
])
def test_optional_source_notes_and_valid_statuses_preserve_source_strings(db_path, clock, product_owner, status, note):
    service, BriefInput, ReferenceInput = components(db_path, clock)
    p = service.create(product_owner, BriefInput(title="valid"), mode="quick")
    body = " \nreference body😀\u3000 "
    data = ReferenceInput(text=body, source_note=note, source_status=status)
    ref = service.save_reference(product_owner, p.id, 1, None, data)
    assert ref.data.text == body and ref.data.source_note == note and ref.data.source_status == status
    assert read(db_path, "SELECT text,source_note,source_status FROM reference_revisions") == [
        {"text": body, "source_note": note, "source_status": status}]
