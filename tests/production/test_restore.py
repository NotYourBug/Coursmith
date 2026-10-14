"""Recover the complete private source graph, including immutable old pointers."""

import hashlib
import json
from contextlib import closing
from pathlib import Path

from course_platform.database import backup_database, check_database, migrate_database, open_readonly
from course_platform.domain import Actor
from course_platform.production.drafts import DraftService
from course_platform.production.models import ReferenceInput
from course_platform.production.snapshots import SnapshotService
from tests.production.test_drafts import generate
from tests.production.test_schema import PRODUCTION_TABLES, read_m1_graph, seed_v5_graph
from tests.production.test_snapshots import BODY, ready


ROOT = Path(__file__).resolve().parents[2]


def production_graph(path):
    with closing(open_readonly(path)) as connection:
        return {name: [tuple(row) for row in connection.execute(f"SELECT * FROM {name} ORDER BY 1,2")]
                for name in sorted(PRODUCTION_TABLES)}


def test_backup_restore_contains_draft_reference_and_snapshot_sources(tmp_path, clock):
    db, old, backup, restored = [tmp_path / name for name in ("source.db", "before6.db", "backup.db", "restored.db")]
    migrate_database(db, through_version=5)
    m1 = seed_v5_graph(db)
    migrate_database(db, backup_path=old)
    assert check_database(old)["version"] == 5 and read_m1_graph(old) == m1
    owner = Actor(1, "restore-test")
    r = ready(db, clock, owner, mode="precise")
    original = r.drafts.freeze_input(r.project_id)
    snapshot = r.snapshots.create(owner, r.project_id, original.project_revision)
    # Future pointers differ: restoring only current sources would lose this handoff.
    r.drafts.save_chapter(owner, r.project_id, r.chapter_id, r.body_revision, BODY + "新修订")
    reference = original.references[0]
    r.projects.save_reference(owner, r.project_id, r.projects.get(r.project_id).revision,
        reference.id, ReferenceInput(text="新资料", source_note="自有笔记", source_status="owner_noted"))
    current = r.drafts.freeze_input(r.project_id).chapters[0]
    candidate = generate(db, clock, owner, r.drafts, r.project_id, current.id,
                         current.body_revision, BODY + "未采用候选")
    assert candidate.candidate_id is not None
    before, business = production_graph(db), read_m1_graph(db)
    for table in m1:
        # Production appends safe audit entries, but never changes its old prefix.
        assert business[table][:len(m1[table])] == m1[table]
    backup_database(db, backup)
    # Use SQLite backup again to reserve a separate recovery target; no hot file copy.
    backup_database(backup, restored)
    assert production_graph(backup) == production_graph(restored) == before
    assert read_m1_graph(restored) == business
    assert check_database(restored) == check_database(backup)
    with closing(open_readonly(restored)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        row = connection.execute("SELECT * FROM production_snapshots WHERE id=?", (snapshot.id,)).fetchone()
        refs = connection.execute("SELECT * FROM production_snapshot_refs WHERE snapshot_id=? ORDER BY position", (snapshot.id,)).fetchall()
        assert row["chapter_count"] == 2 and row["reference_count"] == 1 and len(refs) == 3
        assert all(ref["chapter_revision_id"] is not None or ref["reference_revision_id"] is not None for ref in refs)
    service = SnapshotService(restored, DraftService(restored, clock=clock.now), clock=clock.now)
    recovered = service.get(owner, snapshot.id)
    assert recovered == snapshot and service.read_snapshot_payload(snapshot.id) == original
    assert hashlib.sha256(recovered.payload_json.encode()).hexdigest() == snapshot.hash
    payload = json.loads(recovered.payload_json)
    assert payload["input_hash"] == original.input_hash
    assert [c["id"] for c in payload["chapters"]] == [c.id for c in original.chapters]
    assert payload["references"][0]["revision"] == 1
    assert payload["references"][0]["data"]["text"] == " 原文\r\n😀 e\u0301 "
    assert recovered.check.human_review_required and not recovered.check.blocking
    # Historical trusted reads neither audit nor follow new current pointers.
    assert production_graph(restored) == before and read_m1_graph(restored) == business
    assert not list(tmp_path.rglob("*.html")), "Recovery must not publish private source files"
    assert (ROOT / "docs/operations/m2a-runbook.md").is_file(), "M2A recovery operator handoff is missing"
