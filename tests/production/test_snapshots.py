"""Immutable handoff contracts using real source rows and protected HTTP routes."""

import hashlib
import importlib
import json
import sqlite3
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from course_platform.database import connect, transaction
from course_platform.domain import Actor, BusinessError
from course_platform.production.drafts import DraftService
from course_platform.production.models import BriefInput, OutlineInput, OutlineItem, ReferenceInput
from course_platform.production.projects import ProjectService
from tests.production.test_drafts import finish_job, generate, seed_job
from tests.production.test_routes import post


BODY = "## 学习目标\n学习中文😀 e\u0301\n\n## 讲解\n保留 空格  \n\n## 案例\n非技术案例\n\n## 练习\n写下思考\n\n## 总结\n回顾\n"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def read(path, sql, values=()):
    with transaction(path) as conn:
        return [dict(row) for row in conn.execute(sql, values)]


class Ready(SimpleNamespace):
    @property
    def snapshots(self):
        # Missing implementation must fail in a test body, never fixture setup.
        module = importlib.import_module("course_platform.production.snapshots")
        return module.SnapshotService(self.db_path, self.drafts, clock=self.clock.now)


def ready(path, clock, owner, *, count=2, mode="quick", references=True):
    projects, drafts = ProjectService(path, clock=clock.now), DraftService(path, clock=clock.now)
    project = projects.create(owner, BriefInput(title="原始题目 e\u0301 😀", audience="  受众\n", chapter_count=count), mode=mode)
    if references:
        projects.save_reference(owner, project.id, project.revision, None,
            ReferenceInput(text=" 原文\r\n😀 e\u0301 ", source_note=" 自有笔记 ", source_status="owner_noted"))
    outline = drafts.save_outline(owner, project.id, projects.get(project.id).revision, OutlineInput(items=[
        OutlineItem(chapter_id=None, title=f"第{i}章", goal="目标") for i in range(count)]))
    for item in outline.items:
        drafts.save_chapter(owner, project.id, item.chapter_id, None, BODY)
    if mode == "precise":
        drafts.confirm_outline(owner, project.id, projects.get(project.id).revision)
    frozen = drafts.freeze_input(project.id)
    return Ready(db_path=path, clock=clock, actor=owner, projects=projects, drafts=drafts,
        project_id=project.id, project_revision=frozen.project_revision,
        chapter_id=frozen.chapters[0].id, body_revision=frozen.chapters[0].body_revision)


@pytest.fixture
def snapshot_ready(db_path, clock, product_owner):
    return ready(db_path, clock, product_owner)


def test_snapshot_hash_does_not_change_after_edit(snapshot_ready):
    r = snapshot_ready
    original = r.drafts.freeze_input(r.project_id)
    before = r.snapshots.create(r.actor, r.project_id, r.project_revision)
    r.drafts.save_chapter(r.actor, r.project_id, r.chapter_id, r.body_revision, BODY + "人工新正文")
    after = r.snapshots.get(r.actor, before.id)
    assert after.hash == before.hash and after.payload_json == before.payload_json
    assert r.snapshots.read_snapshot_payload(before.id) == original
    assert after.check.input_hash == original.input_hash and after.check.human_review_required is True
    assert after.check.blocking is False
    assert hashlib.sha256(before.payload_json.encode("utf-8")).hexdigest() == before.hash
    assert before.hash != original.input_hash
    payload = json.loads(before.payload_json)
    assert payload["rendering_version"] == "1" and payload["prompt_version"] == "m2a-v1"
    assert payload["brief"]["audience"] == "  受众\n"
    assert payload["chapters"][0]["markdown"] == BODY
    assert payload["references"][0]["data"]["text"] == " 原文\r\n😀 e\u0301 "
    assert before.payload_json == canonical(payload)
    with pytest.raises((AttributeError, TypeError)):
        after.hash = "changed"


@pytest.mark.parametrize("state", ["queued", "running"])
@pytest.mark.parametrize("kind", ["course_draft_generate", "outline_generate", "chapters_generate", "chapter_regenerate"])
def test_snapshot_blocked_by_modifying_job(snapshot_ready, state, kind):
    r = snapshot_ready
    if state == "queued":
        with transaction(r.db_path) as conn:
            job = conn.execute("""INSERT INTO production_jobs
                (project_id,actor_admin_id,kind,project_revision,state,input_hash,input_json,config_json,request_digest)
                VALUES (?,?,?,?,'queued',?,'{}','{}','test')""", (r.project_id, r.actor.admin_id, kind,
                    r.project_revision, r.drafts.freeze_input(r.project_id).input_hash)).lastrowid
        attempt = None
    else:
        job, attempt = seed_job(r.db_path, r.clock, r.actor, r.drafts.freeze_input(r.project_id), kind=kind)
    check = r.snapshots.check(r.actor, r.project_id, r.project_revision)
    assert check.blocking and any(f.code == "modifying_job" for f in check.findings)
    with pytest.raises(BusinessError) as denied:
        r.snapshots.create(r.actor, r.project_id, r.project_revision)
    assert denied.value.code == "snapshot_blocked"
    assert read(r.db_path, "SELECT * FROM production_snapshots") == []
    if attempt is None:
        with transaction(r.db_path) as conn:
            conn.execute("UPDATE production_jobs SET state='cancelled' WHERE id=?", (job,))
    else:
        finish_job(r.db_path, job, attempt)
    assert r.snapshots.create(r.actor, r.project_id, r.project_revision).check.blocking is False


def test_local_validation_job_does_not_block_handoff(snapshot_ready):
    r = snapshot_ready
    seed_job(r.db_path, r.clock, r.actor, r.drafts.freeze_input(r.project_id), kind="draft_validate")
    assert r.snapshots.create(r.actor, r.project_id, r.project_revision).check.blocking is False


@pytest.mark.parametrize("markdown,code", [("", "missing_body"), ("只有正文", "missing_objectives"),
    (BODY + "x" * 204800, "chapter_too_large")], ids=["empty", "structure", "oversized"])
def test_snapshot_blocked_by_invalid_body(snapshot_ready, markdown, code, monkeypatch):
    r = snapshot_ready
    if code == "chapter_too_large":
        # Database refuses oversize rows; exercise the service's frozen-input boundary too.
        actual = r.drafts._freeze_in_tx
        def oversize(conn, project_id):
            frozen = actual(conn, project_id)
            return replace(frozen, chapters=(replace(frozen.chapters[0], markdown=markdown), *frozen.chapters[1:]))
        monkeypatch.setattr(r.drafts, "_freeze_in_tx", oversize)
    else:
        r.drafts.save_chapter(r.actor, r.project_id, r.chapter_id, r.body_revision, markdown)
    with pytest.raises(BusinessError):
        r.snapshots.create(r.actor, r.project_id, r.projects.get(r.project_id).revision)
    assert read(r.db_path, "SELECT * FROM production_snapshots") == []
    if code != "chapter_too_large":
        result = r.snapshots.check(r.actor, r.project_id, r.projects.get(r.project_id).revision)
        assert any(f.code == code and f.blocking for f in result.findings)


def test_snapshot_blocked_by_job_missing_body_or_unacknowledged_change(snapshot_ready):
    r = snapshot_ready
    frozen = r.drafts.freeze_input(r.project_id)
    r.projects.update(r.actor, r.project_id, frozen.project_revision, BriefInput(title="输入改变", chapter_count=2))
    revision = r.projects.get(r.project_id).revision
    with pytest.raises(BusinessError) as denied:
        r.snapshots.create(r.actor, r.project_id, revision)
    assert denied.value.code == "snapshot_blocked"
    r.drafts.acknowledge_changes(r.actor, r.project_id, revision, (r.chapter_id,))
    with pytest.raises(BusinessError):
        r.snapshots.create(r.actor, r.project_id, r.projects.get(r.project_id).revision)
    r.drafts.acknowledge_changes(r.actor, r.project_id, r.projects.get(r.project_id).revision,
        tuple(c.id for c in frozen.chapters))
    snapshot = r.snapshots.create(r.actor, r.project_id, r.projects.get(r.project_id).revision)
    assert snapshot.check.human_review_required is True


def test_snapshot_blocks_pending_candidate_until_explicit_decision(snapshot_ready):
    r = snapshot_ready
    result = generate(r.db_path, r.clock, r.actor, r.drafts, r.project_id, r.chapter_id, r.body_revision, BODY + "候选")
    revision = r.projects.get(r.project_id).revision
    check = r.snapshots.check(r.actor, r.project_id, revision)
    assert any(f.code == "pending_candidate" and f.blocking for f in check.findings)
    with pytest.raises(BusinessError):
        r.snapshots.create(r.actor, r.project_id, revision)
    r.drafts.reject_candidate(r.actor, r.project_id, revision, result.candidate_id)
    assert r.snapshots.create(r.actor, r.project_id, r.projects.get(r.project_id).revision).check.blocking is False


def test_precise_requires_current_confirmation_but_quick_does_not_forge_it(snapshot_ready):
    r = snapshot_ready
    quick = r.snapshots.create(r.actor, r.project_id, r.project_revision)
    assert not r.snapshots.read_snapshot_payload(quick.id).outline.confirmed
    assert read(r.db_path, "SELECT * FROM outline_confirmations") == []
    r.projects.set_mode(r.actor, r.project_id, r.project_revision, "precise")
    revision = r.projects.get(r.project_id).revision
    r.drafts.acknowledge_changes(r.actor, r.project_id, revision, tuple(c.id for c in r.drafts.freeze_input(r.project_id).chapters))
    revision = r.projects.get(r.project_id).revision
    with pytest.raises(BusinessError):
        r.snapshots.create(r.actor, r.project_id, revision)
    r.drafts.confirm_outline(r.actor, r.project_id, revision)
    confirmed = r.snapshots.create(r.actor, r.project_id, r.projects.get(r.project_id).revision)
    assert r.snapshots.read_snapshot_payload(confirmed.id).outline.confirmed is True
    ref = r.drafts.freeze_input(r.project_id).references[0]
    r.projects.save_reference(r.actor, r.project_id, r.projects.get(r.project_id).revision, ref.id, ReferenceInput(text="新参考"))
    r.drafts.acknowledge_changes(r.actor, r.project_id, r.projects.get(r.project_id).revision,
        tuple(c.id for c in r.drafts.freeze_input(r.project_id).chapters))
    with pytest.raises(BusinessError):
        r.snapshots.create(r.actor, r.project_id, r.projects.get(r.project_id).revision)


def test_old_snapshot_keeps_source_and_reference_revisions(snapshot_ready):
    r = snapshot_ready
    original = r.drafts.freeze_input(r.project_id)
    snapshot = r.snapshots.create(r.actor, r.project_id, r.project_revision)
    ref = original.references[0]
    r.projects.save_reference(r.actor, r.project_id, r.project_revision, ref.id,
        ReferenceInput(text="new", source_note="待核查说明", source_status="pending"))
    r.projects.remove_reference(r.actor, r.project_id, r.projects.get(r.project_id).revision, ref.id)
    r.projects.update(r.actor, r.project_id, r.projects.get(r.project_id).revision, BriefInput(title="new brief", chapter_count=1))
    r.drafts.save_outline(r.actor, r.project_id, r.projects.get(r.project_id).revision,
        OutlineInput(items=[OutlineItem(chapter_id=original.chapters[1].id, title="另一章", goal="new")]))
    r.projects.archive(r.actor, r.project_id, r.projects.get(r.project_id).revision)
    assert r.snapshots.get(r.actor, snapshot.id) == snapshot
    assert r.snapshots.read_snapshot_payload(snapshot.id) == original


@pytest.mark.parametrize("references", [True, False])
def test_snapshot_source_ids_and_complete_chains(db_path, clock, product_owner, references):
    # Interleave other projects so public source sequence 1 differs from global row IDs.
    ready(db_path, clock, product_owner)
    r = ready(db_path, clock, product_owner, count=3, references=references)
    snapshot = r.snapshots.create(r.actor, r.project_id, r.project_revision)
    parent = read(db_path, "SELECT * FROM production_snapshots WHERE id=?", (snapshot.id,))[0]
    children = read(db_path, "SELECT * FROM production_snapshot_refs WHERE snapshot_id=? ORDER BY kind,position", (snapshot.id,))
    assert parent["chapter_count"] == 3 and parent["reference_count"] == int(references)
    brief = read(db_path, "SELECT * FROM brief_revisions WHERE project_id=?", (r.project_id,))[0]
    assert parent["brief_revision_id"] == brief["id"] != brief["revision"]
    bodies = [c for c in children if c["kind"] == "chapter"]
    assert [c["position"] for c in bodies] == [1, 2, 3]
    for child, frozen in zip(bodies, r.drafts.freeze_input(r.project_id).chapters):
        row = read(db_path, "SELECT * FROM chapter_revisions WHERE id=?", (child["chapter_revision_id"],))[0]
        assert row["project_id"] == r.project_id and row["chapter_id"] == frozen.id
        assert row["revision"] == frozen.body_revision and row["id"] != row["revision"]
        assert row["content_hash"] == hashlib.sha256(frozen.markdown.encode()).hexdigest()
    assert read(db_path, "PRAGMA foreign_key_check") == []
    check = read(db_path, "SELECT * FROM draft_checks WHERE id=?", (parent["check_id"],))[0]
    assert check["project_id"] == parent["project_id"] and check["project_revision"] == parent["project_revision"]
    assert check["input_hash"] == parent["input_hash"] == snapshot.check.input_hash


def test_check_does_not_create_sales_approval_or_publish_course(snapshot_ready, active_product, fixture_package):
    r = snapshot_ready
    from tests.production.test_schema import read_m1_graph
    before = read_m1_graph(r.db_path)
    files = {p.relative_to(fixture_package): p.read_bytes() for p in fixture_package.rglob("*") if p.is_file()}
    sources_before = {table: read(r.db_path, f"SELECT * FROM {table}") for table in (
        "production_projects", "production_chapters", "brief_revisions", "reference_revisions", "outline_revisions", "outline_confirmations")}
    first = r.snapshots.check(r.actor, r.project_id, r.project_revision)
    assert first.blocking is False and first.human_review_required is True
    snapshot = r.snapshots.create(r.actor, r.project_id, r.project_revision)
    assert snapshot.check == first
    after = read_m1_graph(r.db_path)
    assert {key: rows for key, rows in after.items() if key != "admin_events"} == {key: rows for key, rows in before.items() if key != "admin_events"}
    assert after["admin_events"][:len(before["admin_events"])] == before["admin_events"]
    assert [row[4] for row in after["admin_events"][len(before["admin_events"]):]] == ["production.check", "production.snapshot"]
    assert {p.relative_to(fixture_package): p.read_bytes() for p in fixture_package.rglob("*") if p.is_file()} == files
    assert {table: read(r.db_path, f"SELECT * FROM {table}") for table in sources_before} == sources_before
    assert len(read(r.db_path, "SELECT * FROM draft_checks")) == 2
    for row in read(r.db_path, "SELECT * FROM draft_checks"):
        assert row["human_review_required"] == 1
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(r.db_path) as conn:
                conn.execute("UPDATE draft_checks SET blocking=1 WHERE id=?", (row["id"],))


def test_cross_project_revision_cannot_enter_snapshot(snapshot_ready, monkeypatch):
    r = snapshot_ready
    foreign = ready(r.db_path, r.clock, r.actor)
    foreign_input = foreign.drafts.freeze_input(foreign.project_id)
    monkeypatch.setattr(r.drafts, "_freeze_in_tx", lambda *_: foreign_input)
    with pytest.raises(BusinessError):
        r.snapshots.create(r.actor, r.project_id, r.project_revision)
    assert read(r.db_path, "SELECT * FROM production_snapshots") == []
    assert read(r.db_path, "SELECT * FROM draft_checks") == []


@pytest.mark.parametrize("revision", [True, 1.0, "1", None, -1, 999], ids=["bool", "float", "text", "none", "negative", "stale"])
def test_snapshot_rejects_noncurrent_strict_revision(snapshot_ready, revision):
    r = snapshot_ready
    with pytest.raises(BusinessError) as denied:
        r.snapshots.create(r.actor, r.project_id, revision)
    assert denied.value.code == "stale_revision"
    assert read(r.db_path, "SELECT * FROM production_snapshots") == []


def test_snapshot_service_requires_enabled_project_owner(snapshot_ready):
    r = snapshot_ready
    # The schema permits exactly one owner; an invented actor must never gain access.
    other = Actor(2, "unregistered-owner")
    with pytest.raises(BusinessError) as denied:
        r.snapshots.create(other, r.project_id, r.project_revision)
    assert denied.value.code == "owner_required"
    snapshot = r.snapshots.create(r.actor, r.project_id, r.project_revision)
    with pytest.raises(BusinessError):
        r.snapshots.get(other, snapshot.id)
    with transaction(r.db_path) as conn:
        conn.execute("UPDATE admins SET enabled=0 WHERE id=1")
    for operation in (lambda: r.snapshots.get(r.actor, snapshot.id), lambda: r.snapshots.check(r.actor, r.project_id, r.project_revision)):
        with pytest.raises(BusinessError) as denied:
            operation()
        assert denied.value.code == "owner_required"


def test_snapshot_and_check_roll_back_if_success_audit_fails(snapshot_ready, monkeypatch):
    r = snapshot_ready
    service = r.snapshots
    def fail(*_):
        raise BusinessError("audit_payload", "safe audit failure", 400)
    monkeypatch.setattr(service.drafts, "_success", fail)
    with pytest.raises(BusinessError):
        service.create(r.actor, r.project_id, r.project_revision)
    for table in ("draft_checks", "production_snapshots", "production_snapshot_refs"):
        assert read(r.db_path, f"SELECT * FROM {table}") == []
    events = read(r.db_path, "SELECT * FROM admin_events WHERE action='production.snapshot'")
    assert len(events) == 1 and events[0]["outcome"] == "denied"
    assert json.loads(events[0]["changes_json"]) == {"error_code": "audit_payload"}


def test_snapshot_read_fails_closed_on_tampered_payload(snapshot_ready):
    r = snapshot_ready
    snapshot = r.snapshots.create(r.actor, r.project_id, r.project_revision)
    # Simulate a damaged restored DB, bypassing append guards only in this test DB.
    with transaction(r.db_path) as conn:
        conn.execute("DROP TRIGGER production_snapshots_immutable_update")
        conn.execute("UPDATE production_snapshots SET payload_json='{}' WHERE id=?", (snapshot.id,))
    for operation in (lambda: r.snapshots.get(r.actor, snapshot.id), lambda: r.snapshots.read_snapshot_payload(snapshot.id)):
        with pytest.raises(BusinessError) as denied:
            operation()
        assert denied.value.code == "snapshot_invalid"


def test_safe_native_snapshot_routes_without_model_configuration(production_data, monkeypatch):
    d, client = production_data, production_data.client
    for chapter in d.drafts.freeze_input(d.project_id).chapters:
        d.drafts.save_chapter(d.actor, d.project_id, chapter.id, None, BODY)
    revision = d.projects.get(d.project_id).revision
    from course_platform.production.config import load_production_settings
    client.app.state.production_settings = load_production_settings(replace(client.app.state.settings, api_key=""), {})
    assert client.app.state.production_settings.api_key == "" and client.app.state.production_settings.public.context_tokens is None
    monkeypatch.setattr("course_platform.production.llm.OpenAIModel.__init__", lambda *_: pytest.fail("No local model calls"))
    base = f"/admin/production/{d.project_id}"
    page = client.get(base)
    assert f'action="{base}/check"' in page.text and f'action="{base}/snapshots"' in page.text
    checked = post(client, base + "/check", revision=str(revision))
    assert checked.status_code == 303 and checked.headers["cache-control"] == "no-store"
    created = post(client, base + "/snapshots", revision=str(revision))
    assert created.status_code == 303
    page = client.get(created.headers["location"])
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert "人工待审核" in page.text and "M3A" in page.text and "只读" in page.text
    assert "第" not in json.dumps(read(d.db_path, "SELECT changes_json FROM admin_events"), ensure_ascii=False)
    main = page.text.split('<main id="main">', 1)[1].split("</main>", 1)[0]
    assert "<form" not in main and "/publish" not in main
    assert read(d.db_path, "SELECT * FROM model_calls") == []
    assert read(d.db_path, "SELECT * FROM production_jobs") == []
    assert created.headers["location"] in client.get(base).text
    snapshot_id = read(d.db_path, "SELECT id FROM production_snapshots")[0]["id"]
    foreign = d.projects.create(d.actor, BriefInput(title="foreign"), mode="quick")
    denied = client.get(f"/admin/production/{foreign.id}/snapshots/{snapshot_id}")
    assert denied.status_code == 404 and "学习中文" not in denied.text
    assert client.get(f"{base}/snapshots/{snapshot_id}/payload").status_code == 404
    assert client.get(base + "/check").status_code == 405


@pytest.mark.parametrize("endpoint", ["check", "snapshots"])
@pytest.mark.parametrize("fault,status", [("csrf", 403), ("origin", 403), ("anonymous", 401), ("stale", 409), ("duplicate", 400)])
def test_snapshot_post_security_never_creates_records(production_data, endpoint, fault, status):
    d, client = production_data, production_data.client
    path = f"/admin/production/{d.project_id}/{endpoint}"
    data = {"revision": "2", "csrf_token": client.cookies.get("coursmith_admin_csrf")}
    headers = {"Origin": "http://testserver"}
    if fault == "csrf":
        data["csrf_token"] = "bad"
    elif fault == "origin":
        headers["Origin"] = "https://evil.example"
    elif fault == "anonymous":
        client.cookies.clear()
    elif fault == "stale":
        data["revision"] = "1"
    if fault == "duplicate":
        response = client.post(path, content=b"csrf_token=x&csrf_token=y", headers={**headers, "Content-Type": "application/x-www-form-urlencoded"})
    else:
        response = client.post(path, data=data, headers=headers)
    assert response.status_code == status and response.headers["cache-control"] == "no-store"
    assert "学习中文" not in response.text
    assert read(d.db_path, "SELECT * FROM draft_checks") == [] and read(d.db_path, "SELECT * FROM production_snapshots") == []


def test_check_frozen_input_hash_binds_every_source(snapshot_ready):
    r = snapshot_ready
    frozen = r.drafts.freeze_input(r.project_id)
    # Independent full hash derivation: removing only mutation/decision metadata.
    payload = asdict(frozen)
    payload.pop("project_revision")
    payload.pop("input_hash")
    payload["brief"] = frozen.brief.model_dump()
    payload["references"] = [{"id": ref.id, "revision": ref.revision, "data": ref.data.model_dump()} for ref in frozen.references]
    payload["outline"]["items"] = [item.model_dump() for item in frozen.outline.items]
    payload["outline"].pop("confirmed")
    expected = hashlib.sha256(canonical(payload).encode()).hexdigest()
    result = r.snapshots.check(r.actor, r.project_id, r.project_revision)
    assert result.input_hash == expected
    stored = read(r.db_path, "SELECT * FROM draft_checks")[0]
    assert stored["input_hash"] == expected and stored["human_review_required"] == 1


def test_historical_snapshot_keeps_original_check_after_validator_changes(snapshot_ready, monkeypatch):
    r = snapshot_ready
    original = r.drafts.freeze_input(r.project_id)
    snapshot = r.snapshots.create(r.actor, r.project_id, r.project_revision)
    from course_platform.production.checks import DraftCheck
    from course_platform.production.rendering import Finding
    module = importlib.import_module("course_platform.production.snapshots")
    monkeypatch.setattr(module, "validate_draft", lambda frozen: DraftCheck(frozen.input_hash,
        (Finding("future_rule", None, True),), True))
    assert r.snapshots.get(r.actor, snapshot.id) == snapshot
    assert r.snapshots.read_snapshot_payload(snapshot.id) == original


def test_freeze_recheck_and_structure_validation_hold_same_write_lock(snapshot_ready, monkeypatch):
    r = snapshot_ready
    service = r.snapshots
    actual = r.drafts._freeze_in_tx
    connections = []
    def frozen(conn, project_id):
        assert conn.in_transaction
        connections.append(conn)
        return actual(conn, project_id)
    monkeypatch.setattr(r.drafts, "_freeze_in_tx", frozen)
    module = importlib.import_module("course_platform.production.snapshots")
    validator = module.validate_draft
    observed = []
    def validate(value):
        other = connect(r.db_path)
        try:
            other.execute("PRAGMA busy_timeout=0")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
            observed.append(True)
        finally:
            other.close()
        return validator(value)
    monkeypatch.setattr(module, "validate_draft", validate)
    service.create(r.actor, r.project_id, r.project_revision)
    assert len(connections) >= 2 and all(conn is connections[0] for conn in connections)
    assert observed


def test_absent_body_and_pending_outline_block_handoff(db_path, clock, product_owner):
    from tests.production.test_drafts import generated_outline
    projects, drafts = ProjectService(db_path, clock=clock.now), DraftService(db_path, clock=clock.now)
    project = projects.create(product_owner, BriefInput(title="课程", chapter_count=1), mode="quick")
    outline = drafts.save_outline(product_owner, project.id, project.revision,
        OutlineInput(items=[OutlineItem(chapter_id=None, title="一章", goal="学习")]))
    r = Ready(db_path=db_path, clock=clock, actor=product_owner, projects=projects, drafts=drafts,
        project_id=project.id, project_revision=projects.get(project.id).revision)
    result = r.snapshots.check(r.actor, r.project_id, r.project_revision)
    assert any(f.code == "missing_body" for f in result.findings)
    with pytest.raises(BusinessError):
        r.snapshots.create(r.actor, r.project_id, r.project_revision)
    drafts.save_chapter(r.actor, project.id, outline.items[0].chapter_id, None, BODY)
    models = importlib.import_module("course_platform.production.models")
    generated_outline(db_path, clock, r.actor, drafts, models, project.id)
    result = r.snapshots.check(r.actor, r.project_id, projects.get(project.id).revision)
    assert any(f.code == "pending_candidate" for f in result.findings)
    with pytest.raises(BusinessError):
        r.snapshots.create(r.actor, r.project_id, projects.get(project.id).revision)


def test_stale_direction_acknowledgement_blocks_handoff(snapshot_ready):
    r = snapshot_ready
    r.drafts.acknowledge_changes(r.actor, r.project_id, r.project_revision,
        tuple(c.id for c in r.drafts.freeze_input(r.project_id).chapters))
    with transaction(r.db_path) as conn:
        conn.execute("UPDATE production_chapters SET acknowledged_input_hash=? WHERE id=?", ("a" * 64, r.chapter_id))
    revision = r.projects.get(r.project_id).revision
    result = r.snapshots.check(r.actor, r.project_id, revision)
    assert any(f.code == "unacknowledged_change" and f.chapter_id == r.chapter_id for f in result.findings)
    with pytest.raises(BusinessError):
        r.snapshots.create(r.actor, r.project_id, revision)
    r.drafts.acknowledge_changes(r.actor, r.project_id, revision, (r.chapter_id,))
    assert r.snapshots.create(r.actor, r.project_id, r.projects.get(r.project_id).revision).check.human_review_required


@pytest.mark.parametrize("table", ["reference_revisions", "chapter_revisions"])
def test_historical_payload_checks_retained_source_hashes(snapshot_ready, table):
    r = snapshot_ready
    snapshot = r.snapshots.create(r.actor, r.project_id, r.project_revision)
    with transaction(r.db_path) as conn:
        conn.execute(f"DROP TRIGGER {table}_immutable_update")
        conn.execute(f"UPDATE {table} SET content_hash=? WHERE project_id=?", ("b" * 64, r.project_id))
    for operation in (lambda: r.snapshots.get(r.actor, snapshot.id), lambda: r.snapshots.read_snapshot_payload(snapshot.id)):
        with pytest.raises(BusinessError) as denied:
            operation()
        assert denied.value.code == "snapshot_invalid"


def test_quick_system_outline_handoff_has_no_human_confirmation(db_path, clock, product_owner):
    projects, drafts = ProjectService(db_path, clock=clock.now), DraftService(db_path, clock=clock.now)
    project = projects.create(product_owner, BriefInput(title="自动草稿", chapter_count=1), mode="quick")
    initial = drafts.freeze_input(project.id)
    job, attempt = seed_job(db_path, clock, product_owner, initial, kind="course_draft_generate")
    with transaction(db_path, immediate=True) as conn:
        outline = drafts.apply_outline_in_tx(conn, project_id=project.id, input_hash=initial.input_hash,
            outline=OutlineInput(items=[OutlineItem(chapter_id=None, title="系统展开", goal="学习")]),
            job_id=job, attempt_id=attempt, auto_adopt=True)
    finish_job(db_path, job, attempt)
    drafts.save_chapter(product_owner, project.id, outline.items[0].chapter_id, None, BODY)
    r = Ready(db_path=db_path, clock=clock, actor=product_owner, drafts=drafts)
    snapshot = r.snapshots.create(product_owner, project.id, projects.get(project.id).revision)
    assert not r.snapshots.read_snapshot_payload(snapshot.id).outline.confirmed
    assert read(db_path, "SELECT * FROM outline_confirmations") == []
    source = read(db_path, "SELECT * FROM outline_revisions WHERE id=?", (outline.id,))[0]
    assert source["source"] == "model" and source["job_id"] == job and source["attempt_id"] == attempt
