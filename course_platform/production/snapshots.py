"""Private immutable draft checks and complete M3A source handoffs.

Snapshot reads prove retained rows, never today's current source pointers.
The trusted payload API deliberately has no public HTTP counterpart.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path

from pydantic import ValidationError

from ..database import open_readonly, to_db_time
from ..domain import Actor, BusinessError, Clock, utc_now
from .checks import DraftCheck, validate_draft
from .config import PROMPT_VERSION
from .drafts import DraftService
from .models import BriefInput, ChapterView, FrozenReference, OutlineItem, OutlineView, ProductionInput, ReferenceInput
from .rendering import Finding

RENDERING_VERSION = "1"


@dataclass(frozen=True)
class SnapshotView:
    id: int
    project_id: int
    hash: str
    payload_json: str
    check: DraftCheck


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _require(condition: bool) -> None:
    if not condition:
        raise BusinessError("snapshot_invalid", "交接源或摘要不一致，请联系维护者核对。", 409)


def _object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


class SnapshotService:
    def __init__(self, db_path: Path, drafts: DraftService, *, clock: Clock = utc_now):
        self.db_path = Path(db_path)
        self.drafts = drafts
        self.clock = clock
        if self.db_path.resolve() != drafts.db_path.resolve():
            raise BusinessError("production_conflict", "制作服务数据库不一致。", 409)

    @staticmethod
    def _owner(connection: sqlite3.Connection, actor: Actor) -> None:
        if not isinstance(actor, Actor) or type(actor.admin_id) is not int or not 1 <= actor.admin_id <= 9223372036854775807 or not connection.execute(
            "SELECT 1 FROM admins WHERE id=? AND role='owner' AND enabled=1", (actor.admin_id,)
        ).fetchone():
            raise BusinessError("owner_required", "需要有效的经营者身份。", 403)

    def _project(self, connection: sqlite3.Connection, actor: Actor, project_id: int):
        self._owner(connection, actor)
        project = self.drafts._row(connection, project_id)
        if project["actor_admin_id"] != actor.admin_id:
            raise BusinessError("project_forbidden", "无法访问此制作项目。", 403)
        return project

    def _editable(self, connection, actor, project_id, revision):
        project = self._project(connection, actor, project_id)
        if type(revision) is not int or revision != project["revision"]:
            raise BusinessError("stale_revision", "页面修订已过期，请重新读取项目。", 409)
        if project["status"] == "archived":
            raise BusinessError("project_archived", "项目已归档，无法创建检查或交接。", 409)
        return project

    @staticmethod
    def _payload(frozen: ProductionInput) -> dict:
        payload = DraftService._source_payload(frozen)
        payload.update(project_revision=frozen.project_revision, input_hash=frozen.input_hash,
            rendering_version=RENDERING_VERSION, prompt_version=PROMPT_VERSION)
        if payload["outline"] is not None:
            payload["outline"]["confirmed"] = frozen.outline.confirmed
        return payload

    @staticmethod
    def _decode(payload: dict) -> ProductionInput:
        _require(type(payload) is dict and set(payload) == {"project_id", "project_revision", "mode",
            "brief_revision", "brief", "references", "outline", "chapters", "input_hash", "rendering_version", "prompt_version"})
        for key in ("project_id", "project_revision", "brief_revision"):
            DraftService._id(payload[key])
        _require(payload["mode"] in ("quick", "precise"))
        DraftService._input_hash(payload["input_hash"])
        for key in ("rendering_version", "prompt_version"):
            _require(type(payload[key]) is str and 1 <= len(payload[key]) <= 128)
        _require(type(payload["brief"]) is dict and set(payload["brief"]) == set(BriefInput.model_fields))
        brief = BriefInput.model_validate(payload["brief"])
        _require(type(payload["references"]) is list and len(payload["references"]) <= 10)
        references = []
        for ref in payload["references"]:
            _require(type(ref) is dict and set(ref) == {"id", "revision", "data"})
            DraftService._id(ref["id"])
            DraftService._id(ref["revision"])
            _require(type(ref["data"]) is dict and set(ref["data"]) == set(ReferenceInput.model_fields))
            references.append(FrozenReference(ref["id"], ref["revision"], ReferenceInput.model_validate(ref["data"])))
        _require(len({r.id for r in references}) == len(references))
        raw_outline = payload["outline"]
        outline = None
        if raw_outline is not None:
            _require(type(raw_outline) is dict and set(raw_outline) == {"id", "revision", "project_id", "items", "confirmed"})
            for key in ("id", "revision", "project_id"):
                DraftService._id(raw_outline[key])
            _require(raw_outline["project_id"] == payload["project_id"] and type(raw_outline["confirmed"]) is bool)
            _require(type(raw_outline["items"]) is list and 1 <= len(raw_outline["items"]) <= 60)
            items = []
            for item in raw_outline["items"]:
                _require(type(item) is dict and set(item) == set(OutlineItem.model_fields))
                DraftService._chapter_id(item["chapter_id"])
                items.append(OutlineItem.model_validate(item))
            _require(len({i.chapter_id for i in items}) == len(items))
            outline = OutlineView(raw_outline["id"], raw_outline["revision"], raw_outline["project_id"], tuple(items), raw_outline["confirmed"])
        _require(type(payload["chapters"]) is list)
        chapters = []
        for chapter in payload["chapters"]:
            _require(type(chapter) is dict and set(chapter) == {"id", "body_revision", "markdown"})
            DraftService._chapter_id(chapter["id"])
            if chapter["body_revision"] is not None:
                DraftService._id(chapter["body_revision"])
            _require(type(chapter["markdown"]) is str)
            chapters.append(ChapterView(**chapter))
        _require([c.id for c in chapters] == [i.chapter_id for i in outline.items] if outline else not chapters)
        frozen = ProductionInput(payload["project_id"], payload["project_revision"], payload["mode"], payload["brief_revision"],
            brief, tuple(references), outline, tuple(chapters), payload["input_hash"])
        _require(_hash(DraftService._source_payload(frozen)) == frozen.input_hash)
        return frozen

    @staticmethod
    def _sources(connection, frozen: ProductionInput):
        """Resolve source sequences to global IDs and prove exact retained text."""
        brief = connection.execute("SELECT * FROM brief_revisions WHERE project_id=? AND revision=?",
            (frozen.project_id, frozen.brief_revision)).fetchone()
        _require(brief is not None and {key: brief[key] for key in BriefInput.model_fields} == frozen.brief.model_dump())
        outline_id = None
        if frozen.outline is not None:
            outline = connection.execute("SELECT * FROM outline_revisions WHERE id=? AND project_id=? AND revision=?",
                (frozen.outline.id, frozen.project_id, frozen.outline.revision)).fetchone()
            _require(outline is not None and outline["item_count"] == len(frozen.outline.items))
            items = connection.execute("SELECT * FROM outline_items WHERE outline_revision_id=? AND project_id=? ORDER BY position",
                (frozen.outline.id, frozen.project_id)).fetchall()
            _require([row["position"] for row in items] == list(range(1, len(frozen.outline.items) + 1)))
            _require([{key: row[key] for key in OutlineItem.model_fields} for row in items] == [item.model_dump() for item in frozen.outline.items])
            outline_id = outline["id"]
            if frozen.outline.confirmed:
                direction = DraftService._source_payload(frozen)
                direction.pop("mode")
                direction.pop("chapters")
                _require(connection.execute("SELECT 1 FROM outline_confirmations WHERE project_id=? AND outline_revision_id=? AND input_hash=?",
                    (frozen.project_id, outline_id, _hash(direction))).fetchone() is not None)
        refs, bodies = [], []
        for ref in frozen.references:
            source = connection.execute("SELECT * FROM reference_revisions WHERE project_id=? AND reference_id=? AND revision=?",
                (frozen.project_id, ref.id, ref.revision)).fetchone()
            _require(source is not None and {key: source[key] for key in ReferenceInput.model_fields} == ref.data.model_dump())
            _require(source["content_hash"] == _hash(ref.data.model_dump()))
            refs.append(source["id"])
        for chapter in frozen.chapters:
            if chapter.body_revision is None:
                _require(chapter.markdown == "")
                bodies.append(None)
                continue
            source = connection.execute("SELECT * FROM chapter_revisions WHERE project_id=? AND chapter_id=? AND revision=?",
                (frozen.project_id, chapter.id, chapter.body_revision)).fetchone()
            _require(source is not None and source["markdown"] == chapter.markdown)
            _require(source["content_hash"] == hashlib.sha256(chapter.markdown.encode("utf-8")).hexdigest())
            bodies.append(source["id"])
        return brief["id"], outline_id, tuple(bodies), tuple(refs)

    def _current(self, connection, project, frozen, sources):
        _require(frozen.project_id == project["id"] and frozen.project_revision == project["revision"] and frozen.mode == project["mode"])
        _require(sources[:2] == (project["current_brief_revision_id"], project["current_outline_revision_id"]))
        refs = connection.execute("SELECT current_revision_id FROM project_reference_links WHERE project_id=? AND active=1 ORDER BY position,reference_id",
            (project["id"],)).fetchall()
        _require(tuple(row["current_revision_id"] for row in refs) == sources[3])
        for chapter, source_id in zip(frozen.chapters, sources[2]):
            row = self.drafts._chapter(connection, project["id"], chapter.id)
            _require(row["current_revision_id"] == source_id)

    def _checked(self, connection, project):
        frozen = self.drafts._freeze_in_tx(connection, project["id"])
        # Re-validate the whole declared representation, including its full hash.
        frozen = self._decode(self._payload(frozen))
        sources = self._sources(connection, frozen)
        self._current(connection, project, frozen, sources)
        result = validate_draft(frozen)
        findings = list(result.findings)
        if connection.execute("""SELECT 1 FROM production_jobs WHERE project_id=? AND state IN ('queued','running')
            AND kind IN ('course_draft_generate','outline_generate','chapters_generate','chapter_regenerate')""", (project["id"],)).fetchone():
            findings.append(Finding("modifying_job", None, True))
        if connection.execute("SELECT 1 FROM production_candidates WHERE project_id=? AND state='pending'", (project["id"],)).fetchone():
            findings.append(Finding("pending_candidate", None, True))
        direction = self.drafts._direction_hash(connection, project, frozen.outline)
        flags = connection.execute("SELECT id,needs_review,review_reason,acknowledged_input_hash FROM production_chapters WHERE project_id=? AND active=1", (project["id"],)).fetchall()
        for row in flags:
            if row["needs_review"] or row["review_reason"] is not None or row["acknowledged_input_hash"] not in (None, direction):
                findings.append(Finding("unacknowledged_change", row["id"], True))
        if project["acknowledged_input_hash"] not in (None, direction):
            findings.append(Finding("unacknowledged_change", None, True))
        rechecked = self.drafts._freeze_in_tx(connection, project["id"])
        _require(self._payload(rechecked) == self._payload(frozen))
        return frozen, sources, DraftCheck(frozen.input_hash, tuple(findings), any(f.blocking for f in findings))

    def _append_check(self, connection, actor, frozen, result):
        return connection.execute("""INSERT INTO draft_checks
            (project_id,project_revision,input_hash,findings_json,blocking,human_review_required,actor_admin_id,created_at)
            VALUES (?,?,?,?,?,1,?,?)""", (frozen.project_id, frozen.project_revision, frozen.input_hash,
                _canonical([asdict(f) for f in result.findings]), int(result.blocking), actor.admin_id, to_db_time(self.clock()))).lastrowid

    def check(self, actor: Actor, project_id: int, revision: int) -> DraftCheck:
        with self.drafts._write(actor, "production.check", project_id) as connection:
            project = self._editable(connection, actor, project_id, revision)
            frozen, _, result = self._checked(connection, project)
            self._append_check(connection, actor, frozen, result)
            self.drafts._success(connection, actor, "production.check", project_id, revision)
            return result

    def create(self, actor: Actor, project_id: int, revision: int) -> SnapshotView:
        with self.drafts._write(actor, "production.snapshot", project_id) as connection:
            project = self._editable(connection, actor, project_id, revision)
            frozen, sources, result = self._checked(connection, project)
            if result.blocking:
                raise BusinessError("snapshot_blocked", "交接仍有阻断项，请检查结构、停止修改任务并明确处理输入变更和候选。", 409)
            payload_json = _canonical(self._payload(frozen))
            snapshot_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            previous = connection.execute("SELECT * FROM production_snapshots WHERE project_id=? AND snapshot_hash=?",
                (project_id, snapshot_hash)).fetchone()
            if previous is not None:
                view, _ = self._read(connection, previous)
                self.drafts._success(connection, actor, "production.snapshot", project_id, revision)
                return view
            check_id = self._append_check(connection, actor, frozen, result)
            snapshot_id = connection.execute("""INSERT INTO production_snapshots
                (project_id,project_revision,chapter_count,reference_count,brief_revision_id,outline_revision_id,
                 check_id,input_hash,snapshot_hash,payload_json,rendering_version,prompt_version,actor_admin_id,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (project_id, revision, len(frozen.chapters), len(frozen.references),
                    sources[0], sources[1], check_id, frozen.input_hash, snapshot_hash, payload_json,
                    RENDERING_VERSION, PROMPT_VERSION, actor.admin_id, to_db_time(self.clock()))).lastrowid
            for position, (chapter, source_id) in enumerate(zip(frozen.chapters, sources[2]), 1):
                connection.execute("""INSERT INTO production_snapshot_refs
                    (snapshot_id,project_id,kind,chapter_id,chapter_revision_id,position) VALUES (?,?,'chapter',?,?,?)""",
                    (snapshot_id, project_id, chapter.id, source_id, position))
            for position, source_id in enumerate(sources[3], 1):
                connection.execute("""INSERT INTO production_snapshot_refs
                    (snapshot_id,project_id,kind,reference_revision_id,position) VALUES (?,?,'reference',?,?)""",
                    (snapshot_id, project_id, source_id, position))
            # Semantic/read proof before exposing; schema6 also enforces full chains at COMMIT.
            view, _ = self._read(connection, self._row(connection, snapshot_id))
            self.drafts._success(connection, actor, "production.snapshot", project_id, revision)
            return view

    @staticmethod
    def _row(connection, snapshot_id):
        DraftService._id(snapshot_id)
        row = connection.execute("SELECT * FROM production_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise BusinessError("snapshot_missing", "交接快照不存在。", 404)
        return row

    def _read(self, connection, row):
        try:
            payload = json.loads(row["payload_json"], object_pairs_hook=_object,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            _require(_canonical(payload) == row["payload_json"])
            _require(hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest() == row["snapshot_hash"])
            frozen = self._decode(payload)
            _require((frozen.project_id, frozen.project_revision, frozen.input_hash) == (row["project_id"], row["project_revision"], row["input_hash"]))
            _require((payload["rendering_version"], payload["prompt_version"]) == (row["rendering_version"], row["prompt_version"]))
            sources = self._sources(connection, frozen)
            _require(sources[:2] == (row["brief_revision_id"], row["outline_revision_id"]))
            _require(row["chapter_count"] == len(frozen.chapters) and row["reference_count"] == len(frozen.references))
            expected = [("chapter", c.id, rid, None, pos) for pos, (c, rid) in enumerate(zip(frozen.chapters, sources[2]), 1)]
            expected += [("reference", None, None, rid, pos) for pos, rid in enumerate(sources[3], 1)]
            children = connection.execute("SELECT * FROM production_snapshot_refs WHERE snapshot_id=? ORDER BY kind,position", (row["id"],)).fetchall()
            _require(all(child["project_id"] == frozen.project_id for child in children))
            _require([tuple(child[key] for key in ("kind", "chapter_id", "chapter_revision_id", "reference_revision_id", "position")) for child in children] == expected)
            check = connection.execute("SELECT * FROM draft_checks WHERE id=? AND project_id=? AND input_hash=?", (row["check_id"], row["project_id"], row["input_hash"])).fetchone()
            _require(check is not None and check["project_revision"] == frozen.project_revision and check["blocking"] == 0 and check["human_review_required"] == 1)
            raw_findings = json.loads(check["findings_json"], object_pairs_hook=_object,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            _require(type(raw_findings) is list)
            findings = []
            for finding in raw_findings:
                _require(type(finding) is dict and set(finding) == {"code", "chapter_id", "blocking"})
                _require(type(finding["code"]) is str and re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", finding["code"]) is not None)
                _require(finding["chapter_id"] is None or finding["chapter_id"] in {c.id for c in frozen.chapters})
                _require(finding["blocking"] is False)
                findings.append(Finding(**finding))
            # Preserve the original immutable result; future validator rules cannot rewrite history.
            result = DraftCheck(frozen.input_hash, tuple(findings), False)
            return SnapshotView(row["id"], row["project_id"], row["snapshot_hash"], row["payload_json"], result), frozen
        except (ValueError, TypeError, KeyError, UnicodeError, ValidationError, BusinessError):
            raise BusinessError("snapshot_invalid", "交接源或摘要不一致，请联系维护者核对。", 409) from None

    def get(self, actor: Actor, snapshot_id: int) -> SnapshotView:
        with closing(open_readonly(self.db_path)) as connection:
            connection.execute("BEGIN")
            self._owner(connection, actor)
            row = self._row(connection, snapshot_id)
            self._project(connection, actor, row["project_id"])
            return self._read(connection, row)[0]

    def read_snapshot_payload(self, snapshot_id: int) -> ProductionInput:
        """Trusted service/restore consumer only; not an authorization boundary."""
        with closing(open_readonly(self.db_path)) as connection:
            connection.execute("BEGIN")
            return self._read(connection, self._row(connection, snapshot_id))[1]
