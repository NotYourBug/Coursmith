"""Stable private draft sources, precise confirmation and explicit candidates.

Worker methods never begin/commit a transaction. Their caller must validate its
Claim in the SAME immediate transaction before calling; job identity here is
not a lease/token fence. Production deliberately has no dependency on jobs.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import ValidationError

from ..database import open_readonly, to_db_time
from ..domain import Actor, BusinessError, Clock, utc_now
from .briefs import source_text
from .models import (BriefInput, ChapterView, FrozenReference, GeneratedResult, OutlineInput,
                     OutlineItem, OutlineView, ProductionInput, ReferenceInput)
from .projects import ProjectService


def _hash(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _proof(condition: bool) -> None:
    if not condition:
        raise ValueError("Unverified generation input.")


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        _proof(key not in result)
        result[key] = value
    return result


class DraftService(ProjectService):
    def __init__(self, db_path: Path, *, clock: Clock = utc_now):
        super().__init__(db_path, clock=clock)

    @staticmethod
    def _outline_input(outline: OutlineInput) -> OutlineInput:
        if not isinstance(outline, OutlineInput):
            raise BusinessError("invalid_production", "大纲字段无效。", 400)
        return OutlineInput.model_validate(outline.model_dump(warnings=False))

    @staticmethod
    def _chapter_id(chapter_id: str) -> None:
        if type(chapter_id) is not str:
            raise BusinessError("invalid_identifier", "章节编号无效。", 400)
        try:
            valid = str(UUID(chapter_id)) == chapter_id
        except (ValueError, AttributeError):
            valid = False
        if not valid:
            raise BusinessError("invalid_identifier", "章节编号无效。", 400)

    @classmethod
    def _chapter(cls, connection: sqlite3.Connection, project_id: int, chapter_id: str) -> sqlite3.Row:
        cls._chapter_id(chapter_id)
        row = connection.execute("SELECT * FROM production_chapters WHERE id=? AND project_id=? AND active=1",
                                 (chapter_id, project_id)).fetchone()
        if row is None:
            raise BusinessError("chapter_missing", "当前项目的活动章节不存在。", 404)
        # Candidate-only UUIDs do not become editable current chapters.
        if not connection.execute("""SELECT 1 FROM outline_items i JOIN production_projects p
            ON p.current_outline_revision_id=i.outline_revision_id AND p.id=i.project_id
            WHERE p.id=? AND i.chapter_id=?""", (project_id, chapter_id)).fetchone():
            raise BusinessError("chapter_missing", "当前大纲中没有此章节。", 404)
        return row

    @staticmethod
    def _markdown(markdown: str) -> str:
        if type(markdown) is not str:
            raise BusinessError("invalid_production", "章节正文必须是文本。", 400)
        try:
            source_text(markdown)
        except ValueError:
            raise BusinessError("invalid_production", "章节正文包含不支持的字符。", 400) from None
        if len(markdown.encode("utf-8")) > 200 * 1024:
            raise BusinessError("chapter_too_large", "单章正文最多 200 KiB，请缩短后保存。", 400)
        return markdown

    @staticmethod
    def _body(connection: sqlite3.Connection, chapter: sqlite3.Row) -> ChapterView:
        if chapter["current_revision_id"] is None:
            return ChapterView(chapter["id"], None, "")
        row = connection.execute("SELECT * FROM chapter_revisions WHERE id=? AND project_id=? AND chapter_id=?",
            (chapter["current_revision_id"], chapter["project_id"], chapter["id"])).fetchone()
        if row is None:
            raise BusinessError("production_conflict", "章节正文修订缺失。", 409)
        return ChapterView(chapter["id"], row["revision"], row["markdown"])

    @classmethod
    def _body_matches(cls, connection: sqlite3.Connection, chapter: sqlite3.Row, body_revision: int | None) -> bool:
        if body_revision is not None and (type(body_revision) is not int or not 1 <= body_revision <= 9223372036854775807):
            raise BusinessError("invalid_revision", "章节正文修订无效。", 400)
        return cls._body(connection, chapter).body_revision == body_revision

    @classmethod
    def _base_id(cls, connection: sqlite3.Connection, project_id: int, chapter_id: str, body_revision: int | None) -> int | None:
        if body_revision is None:
            return None
        if type(body_revision) is not int or not 1 <= body_revision <= 9223372036854775807:
            raise BusinessError("invalid_revision", "章节正文修订无效。", 400)
        base = connection.execute("SELECT id FROM chapter_revisions WHERE project_id=? AND chapter_id=? AND revision=?",
            (project_id, chapter_id, body_revision)).fetchone()
        if base is None:
            raise BusinessError("stale_body_revision", "章节基准修订不存在，请重新读取。", 409)
        return base["id"]

    @classmethod
    def _references(cls, connection: sqlite3.Connection, project_id: int) -> tuple[FrozenReference, ...]:
        rows = connection.execute("""SELECT r.* FROM project_reference_links l JOIN reference_revisions r
            ON r.id=l.current_revision_id AND r.project_id=l.project_id AND r.reference_id=l.reference_id
            WHERE l.project_id=? AND l.active=1 ORDER BY l.position,l.reference_id""", (project_id,)).fetchall()
        return tuple(FrozenReference(r["reference_id"], r["revision"],
            ReferenceInput(**{key: r[key] for key in ReferenceInput.model_fields})) for r in rows)

    @staticmethod
    def _outline_payload(outline: OutlineView | None) -> dict | None:
        if outline is None:
            return None
        return {"id": outline.id, "revision": outline.revision, "project_id": outline.project_id,
                "items": [item.model_dump() for item in outline.items]}

    @classmethod
    def _direction_hash(cls, connection: sqlite3.Connection, project: sqlite3.Row, outline: OutlineView | None) -> str:
        view = cls._view(connection, project)
        return _hash({"project_id": project["id"], "brief_revision": view.brief_revision,
            "brief": view.brief.model_dump(), "references": [
                {"id": r.id, "revision": r.revision, "data": r.data.model_dump()}
                for r in cls._references(connection, project["id"])],
            "outline": cls._outline_payload(outline)})

    @classmethod
    def _outline_view(cls, connection: sqlite3.Connection, project: sqlite3.Row,
                      outline_id: int | None = None) -> OutlineView | None:
        oid = outline_id if outline_id is not None else project["current_outline_revision_id"]
        if oid is None:
            return None
        row = connection.execute("SELECT * FROM outline_revisions WHERE id=? AND project_id=?", (oid, project["id"])).fetchone()
        if row is None:
            raise BusinessError("production_conflict", "大纲修订缺失。", 409)
        items = tuple(OutlineItem(chapter_id=r["chapter_id"], title=r["title"], goal=r["goal"])
            for r in connection.execute("SELECT * FROM outline_items WHERE outline_revision_id=? AND project_id=? ORDER BY position",
                (oid, project["id"])))
        if len(items) != row["item_count"]:
            raise BusinessError("production_conflict", "大纲内容不完整。", 409)
        view = OutlineView(oid, row["revision"], project["id"], items, False)
        confirmation = connection.execute("SELECT * FROM outline_confirmations WHERE id=? AND project_id=? AND outline_revision_id=?",
            (project["current_confirmation_id"], project["id"], oid)).fetchone()
        confirmed = confirmation is not None and oid == project["current_outline_revision_id"] and confirmation["input_hash"] == cls._direction_hash(connection, project, view)
        return OutlineView(view.id, view.revision, view.project_id, items, confirmed)

    @classmethod
    def _freeze_in_tx(cls, connection: sqlite3.Connection, project_id: int) -> ProductionInput:
        project = cls._row(connection, project_id)
        view = cls._view(connection, project)
        refs = cls._references(connection, project_id)
        outline = cls._outline_view(connection, project)
        chapters = tuple(cls._body(connection, cls._chapter(connection, project_id, item.chapter_id)) for item in outline.items) if outline else ()
        payload = {"project_id": project_id, "mode": view.mode, "brief_revision": view.brief_revision,
            "brief": view.brief.model_dump(), "references": [
                {"id": r.id, "revision": r.revision, "data": r.data.model_dump()} for r in refs],
            "outline": cls._outline_payload(outline), "chapters": [asdict(c) for c in chapters]}
        return ProductionInput(project_id, view.revision, view.mode, view.brief_revision,
                               view.brief, refs, outline, chapters, _hash(payload))

    @classmethod
    def _source_payload(cls, frozen: ProductionInput) -> dict:
        return {"project_id": frozen.project_id, "mode": frozen.mode, "brief_revision": frozen.brief_revision,
            "brief": frozen.brief.model_dump(), "references": [
                {"id": r.id, "revision": r.revision, "data": r.data.model_dump()} for r in frozen.references],
            "outline": cls._outline_payload(frozen.outline), "chapters": [asdict(c) for c in frozen.chapters]}

    @classmethod
    def _job_payload(cls, connection: sqlite3.Connection, job: sqlite3.Row) -> dict:
        """Verify full frozen JSON against its immutable hash and source rows."""
        raw = json.loads(job["input_json"], object_pairs_hook=_json_object)
        _proof(type(raw) is dict and set(raw) == {
            "project_id", "project_revision", "mode", "brief_revision", "brief",
            "references", "outline", "chapters", "input_hash"})
        _proof(type(raw["project_id"]) is int and raw["project_id"] == job["project_id"])
        _proof(type(raw["project_revision"]) is int and raw["project_revision"] == job["project_revision"])
        _proof(raw["mode"] in ("quick", "precise") and raw["input_hash"] == job["input_hash"])
        cls._id(raw["brief_revision"])
        BriefInput.model_validate(raw["brief"])
        brief = connection.execute("SELECT * FROM brief_revisions WHERE project_id=? AND revision=?",
            (job["project_id"], raw["brief_revision"])).fetchone()
        _proof(brief is not None and raw["brief"] == {key: brief[key] for key in BriefInput.model_fields})
        _proof(type(raw["references"]) is list and len(raw["references"]) <= 10)
        used = set()
        for ref in raw["references"]:
            _proof(type(ref) is dict and set(ref) == {"id", "revision", "data"})
            cls._id(ref["id"])
            cls._id(ref["revision"])
            ReferenceInput.model_validate(ref["data"])
            _proof(ref["id"] not in used)
            used.add(ref["id"])
            source = connection.execute("SELECT * FROM reference_revisions WHERE project_id=? AND reference_id=? AND revision=?",
                (job["project_id"], ref["id"], ref["revision"])).fetchone()
            _proof(source is not None and ref["data"] == {key: source[key] for key in ReferenceInput.model_fields})
        outline = raw["outline"]
        if outline is not None:
            _proof(type(outline) is dict and set(outline) == {"id", "revision", "project_id", "items", "confirmed"})
            _proof(type(outline["confirmed"]) is bool)
            cls._id(outline["id"])
            cls._id(outline["revision"])
            cls._id(outline["project_id"])
            source = cls._outline_view(connection, cls._row(connection, job["project_id"]), outline["id"])
            outline = {key: value for key, value in outline.items() if key != "confirmed"}
            _proof(outline == cls._outline_payload(source))
        _proof(type(raw["chapters"]) is list)
        ids = [item["chapter_id"] for item in outline["items"]] if outline else []
        _proof(len(raw["chapters"]) == len(ids))
        for cid, chapter in zip(ids, raw["chapters"]):
            _proof(type(chapter) is dict and set(chapter) == {"id", "body_revision", "markdown"} and chapter["id"] == cid)
            if chapter["body_revision"] is None:
                _proof(chapter["markdown"] == "")
            else:
                cls._id(chapter["body_revision"])
                source = connection.execute("SELECT markdown FROM chapter_revisions WHERE project_id=? AND chapter_id=? AND revision=?",
                    (job["project_id"], cid, chapter["body_revision"])).fetchone()
                _proof(source is not None and source["markdown"] == chapter["markdown"])
        payload = {key: raw[key] for key in ("project_id", "mode", "brief_revision", "brief", "references", "chapters")}
        payload["outline"] = outline
        _proof(_hash(payload) == job["input_hash"])
        return payload

    @staticmethod
    def _step_source(connection: sqlite3.Connection, step: sqlite3.Row) -> sqlite3.Row:
        """An input hash alone grants no proof: require the exact source binding."""
        table, pointer = ("outline_revisions", "outline_revision_id") if step["kind"] == "outline" else ("chapter_revisions", "chapter_revision_id")
        row = connection.execute(f"SELECT * FROM {table} WHERE id=? AND project_id=? AND job_id=? AND attempt_id=?",
            (step[pointer], step["project_id"], step["job_id"], step["attempt_id"])).fetchone()
        _proof(row is not None and row["source"] == "model" and row["input_hash"] == step["input_hash"])
        if step["kind"] == "chapter":
            _proof(row["chapter_id"] == step["chapter_id"])
        if step["candidate_id"] is not None:
            candidate = connection.execute("SELECT * FROM production_candidates WHERE id=? AND project_id=? AND job_id=? AND attempt_id=?",
                (step["candidate_id"], step["project_id"], step["job_id"], step["attempt_id"])).fetchone()
            _proof(candidate is not None and candidate["kind"] == step["kind"] and candidate[pointer] == row["id"]
                and candidate["chapter_id"] == step["chapter_id"] and candidate["input_hash"] == step["input_hash"])
        return row

    @classmethod
    def _replay_candidate_input(cls, connection: sqlite3.Connection, candidate: sqlite3.Row, payload: dict) -> dict:
        targets = connection.execute("""SELECT * FROM job_steps WHERE job_id=? AND attempt_id=? AND project_id=?
            AND kind='chapter' AND chapter_id=? AND candidate_id=? AND chapter_revision_id=?
            AND input_hash=? AND state='succeeded'""", (candidate["job_id"], candidate["attempt_id"], candidate["project_id"],
            candidate["chapter_id"], candidate["id"], candidate["chapter_revision_id"], candidate["input_hash"])).fetchall()
        _proof(len(targets) == 1)
        target = targets[0]
        steps = connection.execute("""SELECT * FROM job_steps WHERE job_id=? AND attempt_id=? AND project_id=?
            AND step_number<? AND state='succeeded' ORDER BY step_number""",
            (candidate["job_id"], candidate["attempt_id"], candidate["project_id"], target["step_number"])).fetchall()
        for step in steps:
            # Reconstruct every complete frozen input, including ALL bodies.
            # Manual edits are not silently absorbed into a job's derived input.
            _proof(step["input_hash"] == _hash(payload))
            if step["kind"] not in ("outline", "chapter"):
                continue
            source = cls._step_source(connection, step)
            if step["candidate_id"] is not None:
                continue  # A stored candidate never advances a current source.
            if step["kind"] == "outline":
                _proof(payload["outline"] is None and payload["mode"] == "quick")
                outline = cls._outline_view(connection, cls._row(connection, candidate["project_id"]), source["id"])
                payload["outline"] = cls._outline_payload(outline)
                payload["chapters"] = [{"id": item.chapter_id, "body_revision": None, "markdown": ""} for item in outline.items]
            else:
                matching = [chapter for chapter in payload["chapters"] if chapter["id"] == step["chapter_id"]]
                _proof(len(matching) == 1 and matching[0]["body_revision"] is None)
                matching[0].update(body_revision=source["revision"], markdown=source["markdown"])
        cls._step_source(connection, target)
        _proof(_hash(payload) == candidate["input_hash"])
        return payload

    @classmethod
    def _check_candidate_input(cls, connection: sqlite3.Connection, candidate: sqlite3.Row) -> None:
        """Human adoption depends on direction plus its own body, not other bodies."""
        try:
            source = connection.execute("SELECT * FROM chapter_revisions WHERE id=? AND project_id=? AND chapter_id=?",
                (candidate["chapter_revision_id"], candidate["project_id"], candidate["chapter_id"])).fetchone()
            _proof(source is not None and source["source"] == "model" and source["input_hash"] == candidate["input_hash"]
                and source["job_id"] == candidate["job_id"] and source["attempt_id"] == candidate["attempt_id"])
            current = cls._source_payload(cls._freeze_in_tx(connection, candidate["project_id"]))
            if _hash(current) == candidate["input_hash"]:
                return
            job = connection.execute("""SELECT j.* FROM production_jobs j JOIN job_attempts a ON a.job_id=j.id
                WHERE j.id=? AND j.project_id=? AND a.id=?""",
                (candidate["job_id"], candidate["project_id"], candidate["attempt_id"])).fetchone()
            _proof(job is not None and source["actor_admin_id"] == job["actor_admin_id"])
            expected = cls._job_payload(connection, job)
            if candidate["input_hash"] != job["input_hash"]:
                expected = cls._replay_candidate_input(connection, candidate, expected)
            own = [chapter for chapter in expected["chapters"] if chapter["id"] == candidate["chapter_id"]]
            _proof(len(own) == 1 and cls._base_id(connection, candidate["project_id"], candidate["chapter_id"],
                own[0]["body_revision"]) == candidate["base_body_revision_id"])
            direction_keys = ("project_id", "mode", "brief_revision", "brief", "references", "outline")
            _proof({key: expected[key] for key in direction_keys} == {key: current[key] for key in direction_keys})
        except (ValueError, TypeError, KeyError, BusinessError, ValidationError):
            raise BusinessError("input_changed", "候选生成输入已变化或无法核对，请重新生成。", 409) from None

    def freeze_input(self, project_id: int) -> ProductionInput:
        with closing(open_readonly(self.db_path)) as connection:
            connection.execute("BEGIN")
            return self._freeze_in_tx(connection, project_id)

    @staticmethod
    def _new_chapter(connection: sqlite3.Connection, project_id: int, now: str) -> str:
        chapter_id = str(uuid4())
        connection.execute("INSERT INTO production_chapters (id,project_id,active,created_at) VALUES (?,?,0,?)",
                           (chapter_id, project_id, now))
        return chapter_id

    def _append_outline(self, connection: sqlite3.Connection, *, project_id: int, items: tuple[OutlineItem, ...],
                        input_hash: str, source: str, actor: Actor, job_id: int | None = None,
                        attempt_id: int | None = None) -> int:
        seq = connection.execute("SELECT COALESCE(MAX(revision),0)+1 FROM outline_revisions WHERE project_id=?", (project_id,)).fetchone()[0]
        oid = connection.execute("""INSERT INTO outline_revisions
            (project_id,revision,item_count,input_hash,source,actor_admin_id,job_id,attempt_id,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)""", (project_id, seq, len(items), input_hash, source,
                actor.admin_id, job_id, attempt_id, to_db_time(self.clock()))).lastrowid
        for position, item in enumerate(items, 1):
            connection.execute("""INSERT INTO outline_items
                (outline_revision_id,project_id,position,chapter_id,title,goal) VALUES (?,?,?,?,?,?)""",
                (oid, project_id, position, item.chapter_id, item.title, item.goal))
        return oid

    def _activate_outline(self, connection: sqlite3.Connection, project_id: int, outline_id: int) -> None:
        # All items exist before the schema's complete-outline pointer guard.
        connection.execute("UPDATE production_projects SET current_outline_revision_id=? WHERE id=?", (outline_id, project_id))
        # Invalidate the chapters from the prior current outline. A new blank
        # identity has no historical body to review merely because it was created.
        self._advance(connection, project_id)
        connection.execute("""UPDATE production_chapters SET active=CASE WHEN id IN
            (SELECT chapter_id FROM outline_items WHERE outline_revision_id=?) THEN 1 ELSE 0 END WHERE project_id=?""",
            (outline_id, project_id))

    def save_outline(self, actor: Actor, project_id: int, revision: int, outline: OutlineInput) -> OutlineView:
        with self._write(actor, "production.outline", project_id) as connection:
            project = self._editable(connection, project_id, revision)
            data = self._outline_input(outline)
            current = self._outline_view(connection, project)
            allowed = {item.chapter_id for item in current.items} if current else set()
            items = []
            used = set()
            for item in data.items:
                cid = item.chapter_id
                if cid is not None:
                    self._chapter_id(cid)
                    if cid not in allowed or cid in used:
                        raise BusinessError("invalid_outline_mapping", "章节归属或对应关系无效。", 400)
                else:
                    cid = self._new_chapter(connection, project_id, to_db_time(self.clock()))
                used.add(cid)
                items.append(OutlineItem(chapter_id=cid, title=item.title, goal=item.goal))
            oid = self._append_outline(connection, project_id=project_id, items=tuple(items),
                input_hash=self._freeze_in_tx(connection, project_id).input_hash, source="manual", actor=actor)
            self._activate_outline(connection, project_id, oid)
            self._success(connection, actor, "production.outline", project_id, revision + 1)
            return self._outline_view(connection, self._row(connection, project_id))

    def confirm_outline(self, actor: Actor, project_id: int, revision: int) -> OutlineView:
        with self._write(actor, "production.confirm", project_id) as connection:
            project = self._editable(connection, project_id, revision)
            outline = self._outline_view(connection, project)
            if outline is None:
                raise BusinessError("outline_missing", "请先保存大纲。", 409)
            cid = connection.execute("""INSERT INTO outline_confirmations
                (project_id,outline_revision_id,input_hash,actor_admin_id,created_at) VALUES (?,?,?,?,?)""",
                (project_id, outline.id, self._direction_hash(connection, project, outline), actor.admin_id,
                 to_db_time(self.clock()))).lastrowid
            connection.execute("UPDATE production_projects SET current_confirmation_id=? WHERE id=?", (cid, project_id))
            self._advance(connection, project_id, input_changed=False)
            self._success(connection, actor, "production.confirm", project_id, revision + 1)
            return self._outline_view(connection, self._row(connection, project_id))

    @classmethod
    def _body_editable(cls, connection: sqlite3.Connection, project_id: int, chapter_id: str) -> sqlite3.Row:
        project = cls._row(connection, project_id)
        if project["status"] == "archived":
            raise BusinessError("project_archived", "项目已归档，无法编辑。", 409)
        chapter = cls._chapter(connection, project_id, chapter_id)
        jobs = connection.execute("""SELECT * FROM production_jobs WHERE project_id=?
            AND state IN ('queued','running') AND kind IN
            ('course_draft_generate','outline_generate','chapters_generate','chapter_regenerate')""", (project_id,)).fetchall()
        for job in jobs:
            if job["kind"] == "outline_generate":
                continue
            selected = json.loads(job["chapter_ids_json"])
            if job["kind"] == "chapter_regenerate" and chapter_id in selected:
                raise BusinessError("chapter_busy", "此章节正在重做，请待任务停止后编辑。", 409)
            if connection.execute("SELECT 1 FROM job_steps WHERE job_id=? AND chapter_id=? AND state='running'", (job["id"], chapter_id)).fetchone():
                raise BusinessError("chapter_busy", "此章节正在生成，请待任务停止后编辑。", 409)
            # A batch may run another chapter while a completed chapter is edited.
            # Its not-yet-filled selected chapters remain reserved even if their
            # step has not been created by the later worker yet.
            if chapter["current_revision_id"] is None and (not selected or chapter_id in selected):
                raise BusinessError("chapter_busy", "此章节已排队生成，请待任务停止后编辑。", 409)
        return chapter

    def _append_body(self, connection: sqlite3.Connection, *, project_id: int, chapter_id: str,
                     markdown: str, source: str, actor: Actor, input_hash: str,
                     job_id: int | None = None, attempt_id: int | None = None) -> int:
        seq = connection.execute("SELECT COALESCE(MAX(revision),0)+1 FROM chapter_revisions WHERE project_id=? AND chapter_id=?",
                                 (project_id, chapter_id)).fetchone()[0]
        return connection.execute("""INSERT INTO chapter_revisions
            (project_id,chapter_id,revision,markdown,content_hash,input_hash,source,actor_admin_id,job_id,attempt_id,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (project_id, chapter_id, seq, markdown,
                hashlib.sha256(markdown.encode("utf-8")).hexdigest(), input_hash, source, actor.admin_id,
                job_id, attempt_id, to_db_time(self.clock()))).lastrowid

    def save_chapter(self, actor: Actor, project_id: int, chapter_id: str,
                     body_revision: int | None, markdown: str) -> ChapterView:
        with self._write(actor, "production.chapter", project_id) as connection:
            chapter = self._body_editable(connection, project_id, chapter_id)
            if not self._body_matches(connection, chapter, body_revision):
                raise BusinessError("stale_body_revision", "章节正文已更新，请复制输入并重新读取。", 409)
            markdown = self._markdown(markdown)
            rid = self._append_body(connection, project_id=project_id, chapter_id=chapter_id, markdown=markdown,
                source="manual", actor=actor, input_hash=self._freeze_in_tx(connection, project_id).input_hash)
            connection.execute("UPDATE production_chapters SET current_revision_id=? WHERE id=? AND project_id=?", (rid, chapter_id, project_id))
            self._advance(connection, project_id, input_changed=False)
            self._success(connection, actor, "production.chapter", project_id, self._row(connection, project_id)["revision"])
            return self._body(connection, self._chapter(connection, project_id, chapter_id))

    @classmethod
    def _candidate(cls, connection: sqlite3.Connection, project_id: int, candidate_id: int, *, kind: str | None = None) -> sqlite3.Row:
        cls._id(candidate_id)
        row = connection.execute("SELECT * FROM production_candidates WHERE id=? AND project_id=?", (candidate_id, project_id)).fetchone()
        if row is None or (kind is not None and row["kind"] != kind):
            raise BusinessError("candidate_missing", "当前项目的候选不存在。", 404)
        if row["state"] != "pending":
            raise BusinessError("candidate_decided", "候选已处理，请重新读取。", 409)
        return row

    def _decide(self, connection: sqlite3.Connection, actor: Actor, candidate_id: int, state: str) -> None:
        connection.execute("UPDATE production_candidates SET state=?,revision=revision+1,decided_by=?,decided_at=? WHERE id=?",
                           (state, actor.admin_id, to_db_time(self.clock()), candidate_id))

    def adopt_outline(self, actor: Actor, project_id: int, revision: int,
                      candidate_id: int, matches: dict[int, str | None]) -> OutlineView:
        with self._write(actor, "production.candidate", project_id) as connection:
            project = self._editable(connection, project_id, revision)
            candidate = self._candidate(connection, project_id, candidate_id, kind="outline")
            proposed = self._outline_view(connection, project, candidate["outline_revision_id"])
            if type(matches) is not dict or any(type(k) is not int for k in matches) or set(matches) != set(range(1, len(proposed.items) + 1)):
                raise BusinessError("invalid_outline_mapping", "请明确每一项新大纲与旧章节的对应关系。", 400)
            current = self._outline_view(connection, project)
            allowed = {item.chapter_id for item in current.items} if current else set()
            used = set()
            items = []
            for pos, item in enumerate(proposed.items, 1):
                cid = matches[pos]
                if cid is None:
                    cid = item.chapter_id
                else:
                    self._chapter_id(cid)
                    if cid not in allowed or cid in used:
                        raise BusinessError("invalid_outline_mapping", "旧章节只能对应一项，且必须属于当前大纲。", 400)
                used.add(cid)
                items.append(OutlineItem(chapter_id=cid, title=item.title, goal=item.goal))
            if candidate["input_hash"] != self._freeze_in_tx(connection, project_id).input_hash:
                raise BusinessError("input_changed", "生成输入已变化，请重新生成候选。", 409)
            oid = self._append_outline(connection, project_id=project_id, items=tuple(items),
                input_hash=candidate["input_hash"], source="manual", actor=actor)
            self._activate_outline(connection, project_id, oid)
            self._decide(connection, actor, candidate_id, "adopted")
            self._success(connection, actor, "production.candidate", project_id, revision + 1)
            return self._outline_view(connection, self._row(connection, project_id))

    def adopt_candidate(self, actor: Actor, project_id: int, revision: int,
                        candidate_id: int, body_revision: int | None) -> ChapterView:
        with self._write(actor, "production.candidate", project_id) as connection:
            project = self._row(connection, project_id)
            if type(revision) is not int or revision != project["revision"]:
                raise BusinessError("stale_revision", "页面修订已过期，请重新读取。", 409)
            candidate = self._candidate(connection, project_id, candidate_id, kind="chapter")
            chapter = self._body_editable(connection, project_id, candidate["chapter_id"])
            if not self._body_matches(connection, chapter, body_revision) or chapter["current_revision_id"] != candidate["base_body_revision_id"]:
                raise BusinessError("stale_body_revision", "候选基准正文已变化，请比较并重新生成。", 409)
            self._check_candidate_input(connection, candidate)
            source = connection.execute("SELECT * FROM chapter_revisions WHERE id=? AND project_id=? AND chapter_id=?",
                (candidate["chapter_revision_id"], project_id, chapter["id"])).fetchone()
            rid = self._append_body(connection, project_id=project_id, chapter_id=chapter["id"], markdown=source["markdown"],
                source="candidate", actor=actor, input_hash=candidate["input_hash"], job_id=candidate["job_id"], attempt_id=candidate["attempt_id"])
            connection.execute("UPDATE production_chapters SET current_revision_id=? WHERE id=? AND project_id=?", (rid, chapter["id"], project_id))
            self._decide(connection, actor, candidate_id, "adopted")
            self._advance(connection, project_id, input_changed=False)
            self._success(connection, actor, "production.candidate", project_id, revision + 1)
            return self._body(connection, self._chapter(connection, project_id, chapter["id"]))

    def reject_candidate(self, actor: Actor, project_id: int, revision: int, candidate_id: int) -> None:
        with self._write(actor, "production.candidate", project_id) as connection:
            self._editable(connection, project_id, revision)
            self._candidate(connection, project_id, candidate_id)
            self._decide(connection, actor, candidate_id, "rejected")
            self._advance(connection, project_id, input_changed=False)
            self._success(connection, actor, "production.candidate", project_id, revision + 1)

    def acknowledge_changes(self, actor: Actor, project_id: int, revision: int, chapter_ids: tuple[str, ...]) -> None:
        with self._write(actor, "production.acknowledge", project_id) as connection:
            project = self._editable(connection, project_id, revision)
            if type(chapter_ids) is not tuple or not chapter_ids or any(type(cid) is not str for cid in chapter_ids) or len(set(chapter_ids)) != len(chapter_ids):
                raise BusinessError("invalid_identifier", "请选择不同的活动章节。", 400)
            for cid in chapter_ids:
                self._chapter(connection, project_id, cid)
            outline = self._outline_view(connection, project)
            direction = self._direction_hash(connection, project, outline)
            for cid in chapter_ids:
                connection.execute("""UPDATE production_chapters SET needs_review=0,review_reason=NULL,
                    acknowledged_input_hash=? WHERE id=? AND project_id=?""", (direction, cid, project_id))
            if not connection.execute("SELECT 1 FROM production_chapters WHERE project_id=? AND active=1 AND needs_review=1", (project_id,)).fetchone():
                connection.execute("UPDATE production_projects SET acknowledged_input_hash=? WHERE id=?", (direction, project_id))
            self._advance(connection, project_id, input_changed=False)
            self._success(connection, actor, "production.acknowledge", project_id, revision + 1)

    @classmethod
    def _worker_actor(cls, connection: sqlite3.Connection, project_id: int, job_id: int, attempt_id: int) -> Actor:
        if not connection.in_transaction:
            raise BusinessError("production_transaction", "生成保存需要同一有效事务。", 409)
        cls._id(job_id)
        cls._id(attempt_id)
        project = cls._row(connection, project_id)
        row = connection.execute("""SELECT j.actor_admin_id FROM production_jobs j JOIN job_attempts a
            ON a.job_id=j.id JOIN admins owner ON owner.id=j.actor_admin_id
            WHERE j.id=? AND j.project_id=? AND a.id=? AND owner.role='owner' AND owner.enabled=1""",
            (job_id, project_id, attempt_id)).fetchone()
        if row is None:
            raise BusinessError("production_conflict", "生成任务与项目或执行轮次不匹配。", 409)
        if project["status"] == "archived":
            raise BusinessError("project_archived", "项目已归档。", 409)
        return Actor(row["actor_admin_id"], f"production-job-{job_id}-{attempt_id}")

    @staticmethod
    def _input_hash(input_hash: str) -> None:
        if type(input_hash) is not str or len(input_hash) != 64 or any(c not in "0123456789abcdef" for c in input_hash):
            raise BusinessError("invalid_production", "生成输入摘要无效。", 400)

    def apply_outline_in_tx(self, connection: sqlite3.Connection, *, project_id: int, input_hash: str,
                            outline: OutlineInput, job_id: int, attempt_id: int, auto_adopt: bool) -> OutlineView:
        actor = self._worker_actor(connection, project_id, job_id, attempt_id)
        self._input_hash(input_hash)
        if type(auto_adopt) is not bool:
            raise BusinessError("invalid_production", "自动采用选项无效。", 400)
        try:
            data = self._outline_input(outline)
        except ValidationError:
            raise BusinessError("invalid_production", "生成大纲字段无效。", 400) from None
        frozen = self._freeze_in_tx(connection, project_id)
        if input_hash != frozen.input_hash:
            raise BusinessError("input_changed", "生成输入已变化，请重新提交任务。", 409)
        # A model cannot choose existing IDs, even if they were copied from a prompt.
        items = tuple(OutlineItem(chapter_id=self._new_chapter(connection, project_id, to_db_time(self.clock())),
            title=item.title, goal=item.goal) for item in data.items)
        oid = self._append_outline(connection, project_id=project_id, items=items,
            input_hash=input_hash, source="model", actor=actor, job_id=job_id, attempt_id=attempt_id)
        cancelled = connection.execute("SELECT cancel_requested FROM production_jobs WHERE id=?", (job_id,)).fetchone()[0]
        if auto_adopt and frozen.outline is None and frozen.mode == "quick" and not cancelled:
            self._activate_outline(connection, project_id, oid)
            action = "production.outline"
        else:
            connection.execute("""INSERT INTO production_candidates
                (project_id,kind,outline_revision_id,input_hash,job_id,attempt_id,created_at)
                VALUES (?,'outline',?,?,?,?,?)""", (project_id, oid, input_hash, job_id, attempt_id, to_db_time(self.clock())))
            self._advance(connection, project_id, input_changed=False)
            action = "production.candidate"
        self._success(connection, actor, action, project_id, self._row(connection, project_id)["revision"])
        return self._outline_view(connection, self._row(connection, project_id), oid)

    def record_generated_in_tx(self, connection: sqlite3.Connection, *, project_id: int, chapter_id: str,
                              body_revision: int | None, input_hash: str, markdown: str, job_id: int,
                              attempt_id: int, adopt_empty: bool) -> GeneratedResult:
        actor = self._worker_actor(connection, project_id, job_id, attempt_id)
        self._input_hash(input_hash)
        chapter = self._chapter(connection, project_id, chapter_id)
        if type(adopt_empty) is not bool:
            raise BusinessError("invalid_production", "自动采用选项无效。", 400)
        markdown = self._markdown(markdown)
        if not markdown.strip():
            raise BusinessError("invalid_production", "生成正文为空，请重新生成。", 400)
        base_id = self._base_id(connection, project_id, chapter_id, body_revision)
        frozen = self._freeze_in_tx(connection, project_id)
        matches = self._body_matches(connection, chapter, body_revision)
        rid = self._append_body(connection, project_id=project_id, chapter_id=chapter_id, markdown=markdown,
            source="model", actor=actor, input_hash=input_hash, job_id=job_id, attempt_id=attempt_id)
        cancelled = connection.execute("SELECT cancel_requested FROM production_jobs WHERE id=?", (job_id,)).fetchone()[0]
        # No current row is stronger than a current empty-string source. Never
        # overwrite a saved source, including an explicitly saved empty source.
        if adopt_empty and body_revision is None and chapter["current_revision_id"] is None and matches and input_hash == frozen.input_hash and not cancelled:
            connection.execute("UPDATE production_chapters SET current_revision_id=? WHERE id=? AND project_id=?", (rid, chapter_id, project_id))
            kind, candidate_id, action = "adopted", None, "production.chapter"
        else:
            candidate_id = connection.execute("""INSERT INTO production_candidates
                (project_id,kind,chapter_id,chapter_revision_id,base_body_revision_id,input_hash,job_id,attempt_id,created_at)
                VALUES (?,'chapter',?,?,?,?,?,?,?)""", (project_id, chapter_id, rid, base_id, input_hash,
                job_id, attempt_id, to_db_time(self.clock()))).lastrowid
            kind, action = "candidate", "production.candidate"
        self._advance(connection, project_id, input_changed=False)
        self._success(connection, actor, action, project_id, self._row(connection, project_id)["revision"])
        return GeneratedResult(kind, rid, candidate_id)
