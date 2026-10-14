"""Draft identity, source history and candidate contracts on real SQLite."""

import hashlib
import importlib
import json
import warnings
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

import pytest
from pydantic import ValidationError

from course_platform.database import to_db_time, transaction
from course_platform.domain import Actor, BusinessError
from course_platform.production.models import BriefInput, ReferenceInput


def components(db_path, clock):
    # Missing implementation fails in the test body, never at collection/setup.
    models = importlib.import_module("course_platform.production.models")
    drafts = importlib.import_module("course_platform.production.drafts")
    return drafts.DraftService(db_path, clock=clock.now), models


def read(db_path, sql, values=()):
    with transaction(db_path) as conn:
        return [dict(row) for row in conn.execute(sql, values)]


def sources(db_path):
    return {table: read(db_path, f"SELECT * FROM {table} ORDER BY 1") for table in (
        "production_projects", "production_chapters", "outline_revisions", "outline_items",
        "outline_confirmations", "chapter_revisions", "production_candidates")}


def setup(db_path, clock, project_service, owner, *, count=2, mode="precise"):
    drafts, models = components(db_path, clock)
    p = project_service.create(owner, BriefInput(title="指南😀", chapter_count=count), mode=mode)
    outline = drafts.save_outline(owner, p.id, p.revision, models.OutlineInput(items=[
        models.OutlineItem(chapter_id=None, title="相同标题", goal=f"目标{i}") for i in range(count)]))
    return drafts, models, p, outline


def frozen_json(frozen):
    """Persist the complete frozen value, not an opaque schema-valid placeholder."""
    outline = None if frozen.outline is None else {
        "id": frozen.outline.id, "revision": frozen.outline.revision,
        "project_id": frozen.outline.project_id, "confirmed": frozen.outline.confirmed,
        "items": [item.model_dump() for item in frozen.outline.items]}
    return json.dumps({"project_id": frozen.project_id, "project_revision": frozen.project_revision,
        "mode": frozen.mode, "brief_revision": frozen.brief_revision, "brief": frozen.brief.model_dump(),
        "references": [{"id": r.id, "revision": r.revision, "data": r.data.model_dump()} for r in frozen.references],
        "outline": outline, "chapters": [{"id": c.id, "body_revision": c.body_revision, "markdown": c.markdown} for c in frozen.chapters],
        "input_hash": frozen.input_hash}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def seed_job(db_path, clock, owner, frozen, *, kind="chapter_regenerate", chapter_ids=(), current=None,
             input_json=None, input_hash=None):
    """Actual schema6 job/attempt/step rows, without imaginary jobs APIs."""
    now = to_db_time(clock.now())
    with transaction(db_path, immediate=True) as conn:
        job_id = conn.execute("""INSERT INTO production_jobs
            (project_id,actor_admin_id,kind,project_revision,state,input_hash,input_json,
             config_json,chapter_ids_json,request_digest,started_at)
            VALUES (?,?,?,?,'running',?,?,'{}',?,'test',?)""",
            (frozen.project_id, owner.admin_id, kind, frozen.project_revision,
             input_hash if input_hash is not None else frozen.input_hash,
             input_json if input_json is not None else frozen_json(frozen), json.dumps(chapter_ids), now)).lastrowid
        attempt_id = conn.execute("""INSERT INTO job_attempts
            (job_id,attempt_number,attempt_token,worker_id,started_at,heartbeat_at,lease_until)
            VALUES (?,1,?,'local-test',?,?,?)""", (job_id, f"token-{job_id}", now, now,
            "2030-01-01T00:00:00+00:00")).lastrowid
        if current is not None:
            conn.execute("""INSERT INTO job_steps
                (job_id,attempt_id,project_id,step_number,kind,chapter_id,input_hash,state)
                VALUES (?,?,?,1,'chapter',?,?,'running')""",
                (job_id, attempt_id, frozen.project_id, current, frozen.input_hash))
    return job_id, attempt_id


def finish_job(db_path, job_id, attempt_id):
    with transaction(db_path, immediate=True) as conn:
        conn.execute("UPDATE production_jobs SET state='succeeded' WHERE id=?", (job_id,))
        conn.execute("UPDATE job_attempts SET state='succeeded' WHERE id=?", (attempt_id,))


def generate(db_path, clock, owner, drafts, project_id, chapter_id, body_revision, markdown, *, adopt_empty=False):
    frozen = drafts.freeze_input(project_id)
    job, attempt = seed_job(db_path, clock, owner, frozen, chapter_ids=(chapter_id,), current=chapter_id)
    with transaction(db_path, immediate=True) as conn:
        result = drafts.record_generated_in_tx(conn, project_id=project_id, chapter_id=chapter_id,
            body_revision=body_revision, input_hash=frozen.input_hash, markdown=markdown,
            job_id=job, attempt_id=attempt, adopt_empty=adopt_empty)
        conn.execute("""UPDATE job_steps SET state='succeeded',chapter_revision_id=?,candidate_id=?
            WHERE job_id=? AND attempt_id=? AND step_number=1""", (result.revision_id, result.candidate_id, job, attempt))
    finish_job(db_path, job, attempt)
    return result


def generated_outline(db_path, clock, owner, drafts, models, project_id, *, auto_adopt=True):
    frozen = drafts.freeze_input(project_id)
    job, attempt = seed_job(db_path, clock, owner, frozen, kind="outline_generate")
    with transaction(db_path, immediate=True) as conn:
        outline = drafts.apply_outline_in_tx(conn, project_id=project_id, input_hash=frozen.input_hash,
            outline=models.OutlineInput(items=[models.OutlineItem(chapter_id=item.chapter_id,
                title=item.title, goal=item.goal) for item in frozen.outline.items]),
            job_id=job, attempt_id=attempt, auto_adopt=auto_adopt)
        candidate = conn.execute("SELECT id FROM production_candidates WHERE outline_revision_id=?", (outline.id,)).fetchone()
        conn.execute("""INSERT INTO job_steps
            (job_id,attempt_id,project_id,step_number,kind,input_hash,state,outline_revision_id,candidate_id)
            VALUES (?,?,?,1,'outline',?,'succeeded',?,?)""",
            (job, attempt, project_id, frozen.input_hash, outline.id, candidate["id"] if candidate else None))
    finish_job(db_path, job, attempt)
    candidate = read(db_path, "SELECT * FROM production_candidates WHERE outline_revision_id=?", (outline.id,))[0]
    return outline, candidate["id"]


def test_reordering_identical_titles_preserves_chapter_ids(db_path, clock, project_service, product_owner):
    d, m, p, o = setup(db_path, clock, project_service, product_owner)
    a = d.save_chapter(product_owner, p.id, o.items[0].chapter_id, None, "A正文")
    b = d.save_chapter(product_owner, p.id, o.items[1].chapter_id, None, "B正文")
    renamed = m.OutlineItem(chapter_id=b.id, title="改名", goal="新目标")
    after = d.save_outline(product_owner, p.id, project_service.get(p.id).revision,
        m.OutlineInput(items=[renamed, o.items[0]]))
    assert [i.chapter_id for i in after.items] == [b.id, a.id]
    assert [(c.id, c.markdown) for c in d.freeze_input(p.id).chapters] == [(b.id, "B正文"), (a.id, "A正文")]
    assert all(str(UUID(i.chapter_id)) == i.chapter_id for i in after.items)


def test_outline_regeneration_requires_explicit_mapping(db_path, clock, project_service, product_owner):
    d, m, p, o = setup(db_path, clock, project_service, product_owner)
    d.save_chapter(product_owner, p.id, o.items[0].chapter_id, None, "A")
    d.save_chapter(product_owner, p.id, o.items[1].chapter_id, None, "B")
    proposed, cid = generated_outline(db_path, clock, product_owner, d, m, p.id)
    assert set(i.chapter_id for i in proposed.items).isdisjoint(i.chapter_id for i in o.items)
    assert d.freeze_input(p.id).outline.id == o.id
    before = sources(db_path)
    with pytest.raises(BusinessError) as denied:
        d.adopt_outline(product_owner, p.id, project_service.get(p.id).revision, cid, {1: o.items[0].chapter_id})
    assert denied.value.code == "invalid_outline_mapping"
    assert sources(db_path) == before
    adopted = d.adopt_outline(product_owner, p.id, project_service.get(p.id).revision, cid,
        {1: o.items[1].chapter_id, 2: o.items[0].chapter_id})
    assert [i.chapter_id for i in adopted.items] == [o.items[1].chapter_id, o.items[0].chapter_id]
    assert [c.markdown for c in d.freeze_input(p.id).chapters] == ["B", "A"]


@pytest.mark.parametrize("matches", [{0: None, 1: None}, {1: None, 2: None, 3: None}, {True: None, 2: None}])
def test_outline_mapping_positions_are_exact(db_path, clock, project_service, product_owner, matches):
    d, m, p, _ = setup(db_path, clock, project_service, product_owner)
    _, cid = generated_outline(db_path, clock, product_owner, d, m, p.id)
    with pytest.raises(BusinessError):
        d.adopt_outline(product_owner, p.id, project_service.get(p.id).revision, cid, matches)


def test_removed_chapter_history_is_preserved_and_not_reused(db_path, clock, project_service, product_owner):
    d, m, p, o = setup(db_path, clock, project_service, product_owner)
    body = d.save_chapter(product_owner, p.id, o.items[0].chapter_id, None, "历史正文")
    d.save_outline(product_owner, p.id, project_service.get(p.id).revision, m.OutlineInput(items=[o.items[1]]))
    assert read(db_path, "SELECT active FROM production_chapters WHERE id=?", (body.id,))[0]["active"] == 0
    assert read(db_path, "SELECT markdown FROM chapter_revisions WHERE chapter_id=?", (body.id,))[0]["markdown"] == "历史正文"
    with pytest.raises(BusinessError):
        d.save_chapter(product_owner, p.id, body.id, body.body_revision, "误移")


def test_reference_edit_invalidates_precise_confirmation(db_path, clock, project_service, product_owner):
    d, _, p, _ = setup(db_path, clock, project_service, product_owner)
    confirmed = d.confirm_outline(product_owner, p.id, project_service.get(p.id).revision)
    assert confirmed.confirmed is True
    original_hash = read(db_path, "SELECT input_hash FROM outline_confirmations")[0]["input_hash"]
    ref = project_service.save_reference(product_owner, p.id, project_service.get(p.id).revision,
        None, ReferenceInput(text="参考", source_note="来源", source_status="pending"))
    assert d.freeze_input(p.id).outline.confirmed is False
    d.confirm_outline(product_owner, p.id, project_service.get(p.id).revision)
    project_service.save_reference(product_owner, p.id, project_service.get(p.id).revision,
        ref.id, ReferenceInput(text="参考", source_note="来源更改", source_status="pending"))
    assert d.freeze_input(p.id).outline.confirmed is False
    assert read(db_path, "SELECT input_hash FROM outline_confirmations ORDER BY id")[1]["input_hash"] != original_hash


def test_body_edits_preserve_confirmation_and_mode_change_keeps_bodies(db_path, clock, project_service, product_owner):
    d, _, p, o = setup(db_path, clock, project_service, product_owner)
    d.confirm_outline(product_owner, p.id, project_service.get(p.id).revision)
    body = d.save_chapter(product_owner, p.id, o.items[0].chapter_id, None, "正文")
    assert d.freeze_input(p.id).outline.confirmed is True
    project_service.set_mode(product_owner, p.id, project_service.get(p.id).revision, "quick")
    frozen = d.freeze_input(p.id)
    assert frozen.outline.confirmed is False
    assert frozen.chapters[0] == body


def test_stale_save_keeps_current_revision(db_path, clock, project_service, product_owner):
    d, _, p, o = setup(db_path, clock, project_service, product_owner)
    a = d.save_chapter(product_owner, p.id, o.items[0].chapter_id, None, "第一版")
    latest = d.save_chapter(product_owner, p.id, a.id, a.body_revision, "第二版")
    before = sources(db_path)
    with pytest.raises(BusinessError) as denied:
        d.save_chapter(product_owner, p.id, a.id, a.body_revision, "陈旧版")
    assert denied.value.code == "stale_body_revision"
    assert sources(db_path) == before
    assert d.freeze_input(p.id).chapters[0] == latest


def test_body_revision_none_means_no_current_row_even_if_saved_empty(db_path, clock, project_service, product_owner):
    d, _, p, o = setup(db_path, clock, project_service, product_owner)
    blank = d.save_chapter(product_owner, p.id, o.items[0].chapter_id, None, "")
    assert blank.body_revision == 1
    with pytest.raises(BusinessError):
        d.save_chapter(product_owner, p.id, blank.id, None, "覆盖")
    result = generate(db_path, clock, product_owner, d, p.id, blank.id, None, "生成", adopt_empty=True)
    assert result.kind == "candidate"
    assert d.freeze_input(p.id).chapters[0] == blank


def test_regeneration_creates_candidate_not_overwrite(db_path, clock, project_service, product_owner):
    d, _, p, o = setup(db_path, clock, project_service, product_owner)
    body = d.save_chapter(product_owner, p.id, o.items[0].chapter_id, None, "人工正文")
    result = generate(db_path, clock, product_owner, d, p.id, body.id, body.body_revision, "模型候选", adopt_empty=True)
    assert result.kind == "candidate" and result.candidate_id is not None
    assert d.freeze_input(p.id).chapters[0] == body
    adopted = d.adopt_candidate(product_owner, p.id, project_service.get(p.id).revision, result.candidate_id, body.body_revision)
    assert adopted.markdown == "模型候选" and adopted.id == body.id
    assert adopted.body_revision > body.body_revision
    assert read(db_path, "SELECT state FROM production_candidates WHERE id=?", (result.candidate_id,))[0]["state"] == "adopted"


def test_first_body_auto_adopts_without_forging_confirmation(db_path, clock, project_service, product_owner):
    d, _, p, o = setup(db_path, clock, project_service, product_owner, mode="quick")
    result = generate(db_path, clock, product_owner, d, p.id, o.items[0].chapter_id, None, "首次正文", adopt_empty=True)
    assert result.kind == "adopted" and result.candidate_id is None
    assert d.freeze_input(p.id).chapters[0].markdown == "首次正文"
    assert d.freeze_input(p.id).outline.confirmed is False


def test_candidate_from_other_project_cannot_be_adopted(db_path, clock, project_service, product_owner):
    d, _, p, o = setup(db_path, clock, project_service, product_owner)
    _, _, other, _ = setup(db_path, clock, project_service, product_owner)
    result = generate(db_path, clock, product_owner, d, p.id, o.items[0].chapter_id, None, "私有候选")
    before = sources(db_path)
    with pytest.raises(BusinessError):
        d.adopt_candidate(product_owner, other.id, project_service.get(other.id).revision, result.candidate_id, None)
    assert sources(db_path) == before


def test_candidate_cas_and_changed_input_reject_without_losing_sources(db_path, clock, project_service, product_owner):
    d, _, p, o = setup(db_path, clock, project_service, product_owner)
    body = d.save_chapter(product_owner, p.id, o.items[0].chapter_id, None, "基准")
    result = generate(db_path, clock, product_owner, d, p.id, body.id, body.body_revision, "候选")
    latest = d.save_chapter(product_owner, p.id, body.id, body.body_revision, "新编辑")
    before = sources(db_path)
    with pytest.raises(BusinessError) as denied:
        d.adopt_candidate(product_owner, p.id, project_service.get(p.id).revision, result.candidate_id, latest.body_revision)
    assert denied.value.code == "stale_body_revision"
    assert sources(db_path) == before


def test_candidate_reject_is_explicit_and_terminal(db_path, clock, project_service, product_owner):
    d, _, p, o = setup(db_path, clock, project_service, product_owner)
    result = generate(db_path, clock, product_owner, d, p.id, o.items[0].chapter_id, None, "候选")
    d.reject_candidate(product_owner, p.id, project_service.get(p.id).revision, result.candidate_id)
    assert d.freeze_input(p.id).chapters[0].body_revision is None
    before = sources(db_path)
    with pytest.raises(BusinessError):
        d.adopt_candidate(product_owner, p.id, project_service.get(p.id).revision, result.candidate_id, None)
    assert sources(db_path) == before


@pytest.mark.parametrize("text", ["x" * 204800, "😀" * 51200, "中" * 68266 + "xx"], ids=["ascii", "emoji", "chinese"])
def test_200kib_body_is_valid_and_one_byte_more_rejected(db_path, clock, project_service, product_owner, text):
    d, _, p, o = setup(db_path, clock, project_service, product_owner, count=1)
    c = d.save_chapter(product_owner, p.id, o.items[0].chapter_id, None, text)
    assert len(c.markdown.encode("utf-8")) == 204800
    before = sources(db_path)
    with pytest.raises(BusinessError) as denied:
        d.save_chapter(product_owner, p.id, c.id, c.body_revision, c.markdown + "x")
    assert denied.value.code == "chapter_too_large"
    assert sources(db_path) == before


def test_running_job_locks_inputs_and_current_chapter_but_completed_chapter_editable(db_path, clock, project_service, product_owner):
    d, _, p, o = setup(db_path, clock, project_service, product_owner)
    done = d.save_chapter(product_owner, p.id, o.items[0].chapter_id, None, "已完成")
    frozen = d.freeze_input(p.id)
    seed_job(db_path, clock, product_owner, frozen, kind="chapters_generate",
        chapter_ids=(done.id, o.items[1].chapter_id), current=o.items[1].chapter_id)
    with pytest.raises(BusinessError):
        project_service.update(product_owner, p.id, frozen.project_revision, BriefInput(title="改需求"))
    with pytest.raises(BusinessError):
        project_service.save_reference(product_owner, p.id, frozen.project_revision, None, ReferenceInput(text="改资料"))
    with pytest.raises(BusinessError):
        d.save_chapter(product_owner, p.id, o.items[1].chapter_id, None, "当前生成")
    edited = d.save_chapter(product_owner, p.id, done.id, done.body_revision, "完成后编辑")
    assert edited.markdown == "完成后编辑"


def test_outline_edit_requires_project_revision_and_rejects_foreign_ids(db_path, clock, project_service, product_owner):
    d, m, p, o = setup(db_path, clock, project_service, product_owner)
    _, _, other, foreign = setup(db_path, clock, project_service, product_owner)
    before = sources(db_path)
    with pytest.raises(BusinessError):
        d.save_outline(product_owner, p.id, p.revision, m.OutlineInput(items=list(o.items)))
    with pytest.raises(BusinessError):
        d.save_outline(product_owner, p.id, project_service.get(p.id).revision, m.OutlineInput(items=[foreign.items[0]]))
    assert sources(db_path) == before
    assert d.freeze_input(other.id).outline == foreign


def test_acknowledgement_only_clears_explicit_current_chapters(db_path, clock, project_service, product_owner):
    d, _, p, o = setup(db_path, clock, project_service, product_owner)
    d.save_chapter(product_owner, p.id, o.items[0].chapter_id, None, "旧正文")
    project_service.update(product_owner, p.id, project_service.get(p.id).revision, BriefInput(title="新需求"))
    d.acknowledge_changes(product_owner, p.id, project_service.get(p.id).revision, (o.items[0].chapter_id,))
    rows = read(db_path, "SELECT needs_review,acknowledged_input_hash FROM production_chapters WHERE project_id=? ORDER BY rowid", (p.id,))
    assert rows[0]["needs_review"] == 0 and rows[0]["acknowledged_input_hash"] is not None
    assert rows[1]["needs_review"] == 1
    assert d.freeze_input(p.id).outline.confirmed is False
    assert read(db_path, "SELECT current_confirmation_id FROM production_projects WHERE id=?", (p.id,))[0]["current_confirmation_id"] is None


def test_freeze_has_exact_private_sources_and_deterministic_hash_without_writes(db_path, clock, project_service, product_owner):
    d, _, p, o = setup(db_path, clock, project_service, product_owner)
    text = "  e\u0301 中😀\r\n"
    ref = project_service.save_reference(product_owner, p.id, project_service.get(p.id).revision,
        None, ReferenceInput(text=text, source_note=" 来源 ", source_status="owner_noted"))
    d.save_chapter(product_owner, p.id, o.items[0].chapter_id, None, text)
    before = sources(db_path)
    a = d.freeze_input(p.id)
    b = d.freeze_input(p.id)
    assert a == b and len(a.input_hash) == 64
    assert a.references[0].id == ref.id and a.references[0].revision == ref.revision
    assert a.references[0].data.text == a.chapters[0].markdown == text
    assert a.brief_revision == 1 and a.project_revision == project_service.get(p.id).revision
    assert sources(db_path) == before
    # Hash canonical frozen sources, excluding optimistic and confirmation metadata.
    payload = {"project_id": p.id, "mode": "precise", "brief_revision": 1,
        "brief": a.brief.model_dump(), "references": [{"id": ref.id, "revision": 1,
        "data": {"text": text, "source_note": " 来源 ", "source_status": "owner_noted"}}],
        "outline": {"id": o.id, "revision": o.revision, "project_id": p.id,
        "items": [i.model_dump() for i in o.items]},
        "chapters": [{"id": o.items[0].chapter_id, "body_revision": 1, "markdown": text},
                     {"id": o.items[1].chapter_id, "body_revision": None, "markdown": ""}]}
    assert a.input_hash == hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


@pytest.mark.parametrize("bad", [None, Actor(999999, "test"), Actor(True, "test")])
def test_untrusted_actor_does_not_write(db_path, clock, project_service, product_owner, bad):
    d, _, p, o = setup(db_path, clock, project_service, product_owner)
    before = sources(db_path)
    with pytest.raises(BusinessError) as denied:
        d.save_chapter(bad, p.id, o.items[0].chapter_id, None, "私有")
    assert denied.value.code == "owner_required"
    assert sources(db_path) == before


def test_defensive_outline_validation_and_denial_never_leak_private_text(db_path, clock, project_service, product_owner, capsys, caplog):
    d, m, p, _ = setup(db_path, clock, project_service, product_owner)
    sentinel = "PRIVATE-TASK3-OUTLINE"
    invalid = m.OutlineInput.model_construct(items=[m.OutlineItem.model_construct(chapter_id=None, title=[sentinel], goal=sentinel)])
    before = sources(db_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(BusinessError) as denied:
            d.save_outline(product_owner, p.id, project_service.get(p.id).revision, invalid)
    assert caught == []
    assert sentinel not in str(denied.value)
    assert sentinel not in capsys.readouterr().out + caplog.text
    assert sentinel not in json.dumps(read(db_path, "SELECT * FROM admin_events"))
    assert sources(db_path) == before


@pytest.mark.parametrize("items", [[], [{"chapter_id": None, "title": " ", "goal": ""}],
    [{"chapter_id": None, "title": "章", "goal": "x" * 2001}],
    [{"chapter_id": None, "title": "章", "goal": ""}] * 61])
def test_outline_limits_reject_invalid_sources(db_path, clock, items):
    _, m = components(db_path, clock)
    with pytest.raises(ValidationError):
        m.OutlineInput(items=[m.OutlineItem(**item) for item in items])


def test_concurrent_body_edits_have_one_winner(db_path, clock, project_service, product_owner):
    d, _, p, o = setup(db_path, clock, project_service, product_owner)
    first = d.save_chapter(product_owner, p.id, o.items[0].chapter_id, None, "基准")
    barrier = Barrier(2)
    def edit(text):
        barrier.wait()
        try:
            return d.save_chapter(product_owner, p.id, first.id, first.body_revision, text)
        except BusinessError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(edit, ("A", "B")))
    assert sum(isinstance(value, str) and value == "stale_body_revision" for value in outcomes) == 1
    assert len(read(db_path, "SELECT * FROM chapter_revisions WHERE chapter_id=?", (first.id,))) == 2


def test_audit_failure_rolls_back_draft_sources(db_path, clock, project_service, product_owner, monkeypatch):
    d, _, p, o = setup(db_path, clock, project_service, product_owner)
    module = importlib.import_module("course_platform.production.projects")
    before = sources(db_path)
    def fail(*args):
        raise RuntimeError("controlled audit failure")
    monkeypatch.setattr(module, "append_event", fail)
    with pytest.raises(RuntimeError):
        d.save_chapter(product_owner, p.id, o.items[0].chapter_id, None, "不可持久")
    assert sources(db_path) == before


@pytest.mark.parametrize("cancelled", [False, True])
def test_first_quick_outline_is_system_expansion_and_cancelled_result_only_candidate(db_path, clock, project_service, product_owner, cancelled):
    d, m = components(db_path, clock)
    p = project_service.create(product_owner, BriefInput(title="快速", chapter_count=1), mode="quick")
    frozen = d.freeze_input(p.id)
    job, attempt = seed_job(db_path, clock, product_owner, frozen, kind="course_draft_generate")
    with transaction(db_path, immediate=True) as conn:
        if cancelled:
            conn.execute("UPDATE production_jobs SET cancel_requested=1 WHERE id=?", (job,))
        result = d.apply_outline_in_tx(conn, project_id=p.id, input_hash=frozen.input_hash,
            outline=m.OutlineInput(items=[m.OutlineItem(chapter_id="untrusted-model-ID", title="  e\u0301😀  ", goal="\r\n目标")]),
            job_id=job, attempt_id=attempt, auto_adopt=True)
    finish_job(db_path, job, attempt)
    assert str(UUID(result.items[0].chapter_id)) == result.items[0].chapter_id
    assert result.items[0].title == "  e\u0301😀  " and result.items[0].goal == "\r\n目标"
    assert result.confirmed is False
    assert read(db_path, "SELECT * FROM outline_confirmations") == []
    current = d.freeze_input(p.id)
    if cancelled:
        assert current.outline is None and current.chapters == ()
        assert read(db_path, "SELECT state FROM production_candidates")[0]["state"] == "pending"
    else:
        assert current.outline.id == result.id
        assert read(db_path, "SELECT needs_review FROM production_chapters")[0]["needs_review"] == 0


def test_generation_conflict_preserves_candidate_and_does_not_adopt_new_body(db_path, clock, project_service, product_owner):
    d, _, p, o = setup(db_path, clock, project_service, product_owner)
    frozen = d.freeze_input(p.id)
    job, attempt = seed_job(db_path, clock, product_owner, frozen, chapter_ids=(o.items[0].chapter_id,))
    # Simulate an earlier accepted editor transaction before this worker result.
    with transaction(db_path, immediate=True) as conn:
        rid = conn.execute("""INSERT INTO chapter_revisions
            (project_id,chapter_id,revision,markdown,source,actor_admin_id)
            VALUES (?,?,1,'先保存的人工正文','manual',?)""", (p.id, o.items[0].chapter_id, product_owner.admin_id)).lastrowid
        conn.execute("UPDATE production_chapters SET current_revision_id=? WHERE id=?", (rid, o.items[0].chapter_id))
    with transaction(db_path, immediate=True) as conn:
        result = d.record_generated_in_tx(conn, project_id=p.id, chapter_id=o.items[0].chapter_id,
            body_revision=None, input_hash=frozen.input_hash, markdown="模型结果", job_id=job, attempt_id=attempt, adopt_empty=True)
    finish_job(db_path, job, attempt)
    assert result.kind == "candidate"
    assert d.freeze_input(p.id).chapters[0].markdown == "先保存的人工正文"
    assert read(db_path, "SELECT markdown FROM chapter_revisions WHERE id=?", (result.revision_id,))[0]["markdown"] == "模型结果"


def test_reference_change_blocks_candidate_adoption_and_keeps_candidate_history(db_path, clock, project_service, product_owner):
    d, _, p, o = setup(db_path, clock, project_service, product_owner)
    result = generate(db_path, clock, product_owner, d, p.id, o.items[0].chapter_id, None, "旧参考候选")
    project_service.save_reference(product_owner, p.id, project_service.get(p.id).revision, None, ReferenceInput(text="新参考"))
    before = sources(db_path)
    with pytest.raises(BusinessError) as denied:
        d.adopt_candidate(product_owner, p.id, project_service.get(p.id).revision, result.candidate_id, None)
    assert denied.value.code == "input_changed"
    assert sources(db_path) == before


@pytest.mark.parametrize("bad_ids", [(True,), ([],), (None,), ()])
def test_acknowledgement_rejects_malformed_ids_safely(db_path, clock, project_service, product_owner, bad_ids):
    d, _, p, _ = setup(db_path, clock, project_service, product_owner)
    before = sources(db_path)
    with pytest.raises(BusinessError):
        d.acknowledge_changes(product_owner, p.id, project_service.get(p.id).revision, bad_ids)
    assert sources(db_path) == before


@pytest.mark.parametrize("bad_hash", [None, 99, "PRIVATE-HASH-INPUT", "g" * 64])
def test_worker_input_hash_is_validated_before_source_write(db_path, clock, project_service, product_owner, bad_hash):
    d, _, p, o = setup(db_path, clock, project_service, product_owner)
    frozen = d.freeze_input(p.id)
    job, attempt = seed_job(db_path, clock, product_owner, frozen)
    before = sources(db_path)
    with pytest.raises(BusinessError):
        with transaction(db_path, immediate=True) as conn:
            d.record_generated_in_tx(conn, project_id=p.id, chapter_id=o.items[0].chapter_id,
                body_revision=None, input_hash=bad_hash, markdown="候选", job_id=job, attempt_id=attempt, adopt_empty=False)
    assert sources(db_path) == before


def test_two_independent_chapter_candidates_can_both_be_adopted(db_path, clock, project_service, product_owner):
    d, _, p, o = setup(db_path, clock, project_service, product_owner)
    first = d.save_chapter(product_owner, p.id, o.items[0].chapter_id, None, "base A")
    second = d.save_chapter(product_owner, p.id, o.items[1].chapter_id, None, "base B")
    a = generate(db_path, clock, product_owner, d, p.id, first.id, first.body_revision, "candidate A")
    b = generate(db_path, clock, product_owner, d, p.id, second.id, second.body_revision, "candidate B")
    hashes = read(db_path, "SELECT input_hash FROM production_candidates ORDER BY id")
    assert hashes[0]["input_hash"] == hashes[1]["input_hash"]
    assert d.adopt_candidate(product_owner, p.id, project_service.get(p.id).revision, a.candidate_id, first.body_revision).markdown == "candidate A"
    assert d.freeze_input(p.id).chapters[1] == second
    assert d.adopt_candidate(product_owner, p.id, project_service.get(p.id).revision, b.candidate_id, second.body_revision).markdown == "candidate B"
    assert [c.markdown for c in d.freeze_input(p.id).chapters] == ["candidate A", "candidate B"]


def test_completed_chapter_edit_during_other_inflight_call_does_not_stale_candidate(db_path, clock, project_service, product_owner):
    d, _, p, o = setup(db_path, clock, project_service, product_owner)
    first = d.save_chapter(product_owner, p.id, o.items[0].chapter_id, None, "A")
    second = d.save_chapter(product_owner, p.id, o.items[1].chapter_id, None, "B")
    frozen = d.freeze_input(p.id)
    job, attempt = seed_job(db_path, clock, product_owner, frozen, kind="chapters_generate", current=second.id)
    d.save_chapter(product_owner, p.id, first.id, first.body_revision, "A人工修改")
    with transaction(db_path, immediate=True) as conn:
        result = d.record_generated_in_tx(conn, project_id=p.id, chapter_id=second.id,
            body_revision=second.body_revision, input_hash=frozen.input_hash, markdown="B在途结果",
            job_id=job, attempt_id=attempt, adopt_empty=True)
        conn.execute("UPDATE job_steps SET state='succeeded',chapter_revision_id=?,candidate_id=? WHERE job_id=?",
            (result.revision_id, result.candidate_id, job))
    finish_job(db_path, job, attempt)
    assert result.kind == "candidate"
    assert d.adopt_candidate(product_owner, p.id, project_service.get(p.id).revision,
        result.candidate_id, second.body_revision).markdown == "B在途结果"
    assert d.freeze_input(p.id).chapters[0].markdown == "A人工修改"


def derived_candidates(db_path, clock, project_service, owner, *, quick=False, arbitrary_hash=False):
    d, m = components(db_path, clock)
    if quick:
        p = project_service.create(owner, BriefInput(title="快速派生", chapter_count=3), mode="quick")
        first_number = 2
    else:
        d, m, p, _ = setup(db_path, clock, project_service, owner, count=3)
        first_number = 1
    initial = d.freeze_input(p.id)
    job, attempt = seed_job(db_path, clock, owner, initial, kind="course_draft_generate" if quick else "chapters_generate")
    if quick:
        with transaction(db_path, immediate=True) as conn:
            o = d.apply_outline_in_tx(conn, project_id=p.id, input_hash=initial.input_hash,
                outline=m.OutlineInput(items=[m.OutlineItem(chapter_id=None, title=f"章{i}", goal="") for i in range(3)]),
                job_id=job, attempt_id=attempt, auto_adopt=True)
            conn.execute("""INSERT INTO job_steps
                (job_id,attempt_id,project_id,step_number,kind,input_hash,state,outline_revision_id)
                VALUES (?,?,?,1,'outline',?,'succeeded',?)""", (job, attempt, p.id, initial.input_hash, o.id))
    o = d.freeze_input(p.id).outline
    before_first = d.freeze_input(p.id)
    with transaction(db_path, immediate=True) as conn:
        a = d.record_generated_in_tx(conn, project_id=p.id, chapter_id=o.items[0].chapter_id,
            body_revision=None, input_hash=before_first.input_hash, markdown="A自动采用",
            job_id=job, attempt_id=attempt, adopt_empty=True)
        conn.execute("""INSERT INTO job_steps
            (job_id,attempt_id,project_id,step_number,kind,chapter_id,input_hash,state,chapter_revision_id)
            VALUES (?,?,?,?,'chapter',?,?,'succeeded',?)""",
            (job, attempt, p.id, first_number, o.items[0].chapter_id, before_first.input_hash, a.revision_id))
    frozen = d.freeze_input(p.id)
    results = []
    for number, target in enumerate(frozen.chapters[1:], first_number + 1):
        with transaction(db_path, immediate=True) as conn:
            result = d.record_generated_in_tx(conn, project_id=p.id, chapter_id=target.id,
                body_revision=target.body_revision, input_hash="f" * 64 if arbitrary_hash else frozen.input_hash,
                markdown=f"候选{number}", job_id=job, attempt_id=attempt, adopt_empty=False)
            conn.execute("""INSERT INTO job_steps
                (job_id,attempt_id,project_id,step_number,kind,chapter_id,input_hash,state,chapter_revision_id,candidate_id)
                VALUES (?,?,?,?,'chapter',?,?,'succeeded',?,?)""", (job, attempt, p.id, number,
                target.id, "f" * 64 if arbitrary_hash else frozen.input_hash, result.revision_id, result.candidate_id))
            results.append(result)
    finish_job(db_path, job, attempt)
    return d, m, p, o, frozen, job, attempt, results


@pytest.mark.parametrize("quick", [False, True], ids=["body-derived", "quick-outline-derived"])
def test_proved_derived_candidates_ignore_later_unrelated_body_changes(db_path, clock, project_service, product_owner, quick):
    d, _, p, _, frozen, _, _, results = derived_candidates(db_path, clock, project_service, product_owner, quick=quick)
    d.save_chapter(product_owner, p.id, frozen.chapters[0].id, frozen.chapters[0].body_revision, "A后来人工编辑")
    for target, result in zip(frozen.chapters[1:], results):
        assert d.adopt_candidate(product_owner, p.id, project_service.get(p.id).revision,
            result.candidate_id, target.body_revision).markdown.startswith("候选")
    assert d.freeze_input(p.id).chapters[0].markdown == "A后来人工编辑"


@pytest.mark.parametrize("corruption", ["missing", "hash", "project", "job", "attempt", "step_result", "step_input", "unproved_hash"])
def test_unprovable_candidate_inputs_fail_closed_and_keep_history(db_path, clock, project_service, product_owner, corruption):
    d, _, p, _, frozen, job, attempt, results = derived_candidates(db_path, clock, project_service, product_owner,
        quick=True, arbitrary_hash=corruption == "unproved_hash")
    # Mutable step result/status fields may not substitute another output's proof.
    if corruption == "job":
        other_job, other_attempt = seed_job(db_path, clock, product_owner, frozen)
        with transaction(db_path, immediate=True) as conn:
            source = conn.execute("SELECT * FROM production_candidates WHERE id=?", (results[0].candidate_id,)).fetchone()
            cid = conn.execute("""INSERT INTO production_candidates
                (project_id,kind,chapter_id,chapter_revision_id,base_body_revision_id,input_hash,job_id,attempt_id)
                VALUES (?,'chapter',?,?,?,?,?,?)""", (p.id, source["chapter_id"], source["chapter_revision_id"],
                source["base_body_revision_id"], source["input_hash"], other_job, other_attempt)).lastrowid
            results[0] = type(results[0])("candidate", results[0].revision_id, cid)
        finish_job(db_path, other_job, other_attempt)
    elif corruption in ("step_result", "step_input", "attempt"):
        with transaction(db_path, immediate=True) as conn:
            if corruption == "step_result":
                conn.execute("UPDATE job_steps SET chapter_revision_id=NULL WHERE candidate_id=?", (results[0].candidate_id,))
            elif corruption == "step_input":
                conn.execute("UPDATE job_steps SET outline_revision_id=NULL WHERE job_id=? AND step_number=1", (job,))
            else:
                conn.execute("INSERT INTO job_attempts (job_id,attempt_number,attempt_token,worker_id,started_at,heartbeat_at,lease_until,state) VALUES (?,2,'unrelated-attempt','test','now','now','later','succeeded')", (job,))
                other = conn.execute("SELECT id FROM job_attempts WHERE job_id=? AND attempt_number=2", (job,)).fetchone()[0]
                # A second candidate has internally valid FKs but conflicting provenance.
                source = conn.execute("SELECT * FROM production_candidates WHERE id=?", (results[0].candidate_id,)).fetchone()
                cid = conn.execute("""INSERT INTO production_candidates
                    (project_id,kind,chapter_id,chapter_revision_id,base_body_revision_id,input_hash,job_id,attempt_id)
                    VALUES (?,'chapter',?,?,?,?,?,?)""", (p.id, source["chapter_id"], source["chapter_revision_id"],
                    source["base_body_revision_id"], source["input_hash"], job, other)).lastrowid
                results[0] = type(results[0])("candidate", results[0].revision_id, cid)
    elif corruption in ("missing", "hash", "project"):
        # Immutable job input cannot be updated: create a separate schema-valid
        # job/candidate with bad frozen provenance, retaining the original history.
        payload = json.loads(frozen_json(frozen))
        if corruption == "missing":
            payload = {}
        elif corruption == "project":
            payload["project_id"] = p.id + 1000
        other_job, other_attempt = seed_job(db_path, clock, product_owner, frozen,
            input_json=json.dumps(payload), input_hash="e" * 64 if corruption == "hash" else frozen.input_hash)
        with transaction(db_path, immediate=True) as conn:
            results[0] = d.record_generated_in_tx(conn, project_id=p.id, chapter_id=frozen.chapters[1].id,
                body_revision=frozen.chapters[1].body_revision, input_hash=frozen.input_hash, markdown="坏证明候选",
                job_id=other_job, attempt_id=other_attempt, adopt_empty=False)
        finish_job(db_path, other_job, other_attempt)
    d.save_chapter(product_owner, p.id, frozen.chapters[0].id, frozen.chapters[0].body_revision, "A变化")
    before = sources(db_path)
    with pytest.raises(BusinessError) as denied:
        d.adopt_candidate(product_owner, p.id, project_service.get(p.id).revision, results[0].candidate_id,
            frozen.chapters[1].body_revision)
    assert denied.value.code == "input_changed"
    assert sources(db_path) == before


@pytest.mark.parametrize("change", ["brief", "reference", "outline", "mode", "own_body"])
def test_chapter_adoption_still_rejects_direction_and_own_body_changes(db_path, clock, project_service, product_owner, change):
    d, m, p, o = setup(db_path, clock, project_service, product_owner)
    first = d.save_chapter(product_owner, p.id, o.items[0].chapter_id, None, "A")
    second = d.save_chapter(product_owner, p.id, o.items[1].chapter_id, None, "B")
    result = generate(db_path, clock, product_owner, d, p.id, second.id, second.body_revision, "B候选")
    if change == "brief":
        project_service.update(product_owner, p.id, project_service.get(p.id).revision, BriefInput(title="新需求"))
    elif change == "reference":
        project_service.save_reference(product_owner, p.id, project_service.get(p.id).revision, None, ReferenceInput(text="新参考"))
    elif change == "outline":
        d.save_outline(product_owner, p.id, project_service.get(p.id).revision,
            m.OutlineInput(items=[o.items[1], o.items[0]]))
    elif change == "mode":
        project_service.set_mode(product_owner, p.id, project_service.get(p.id).revision, "quick")
    else:
        second = d.save_chapter(product_owner, p.id, second.id, second.body_revision, "B新人工正文")
    before = sources(db_path)
    with pytest.raises(BusinessError) as denied:
        d.adopt_candidate(product_owner, p.id, project_service.get(p.id).revision, result.candidate_id, second.body_revision)
    assert denied.value.code == ("stale_body_revision" if change == "own_body" else "input_changed")
    assert sources(db_path) == before
    assert d.freeze_input(p.id).chapters[0 if change != "outline" else 1] == first
