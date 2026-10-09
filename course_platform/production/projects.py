"""Owner project editing with immutable inputs and atomic current pointers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from pydantic import ValidationError

from ..audit import AuditEvent, append_event
from ..database import open_readonly, to_db_time, transaction
from ..domain import Actor, BusinessError, Clock, utc_now
from .events import record_production_denial
from .models import BriefInput, ProjectView, ReferenceInput, ReferenceView


class ProjectService:
    def __init__(self, db_path: Path, *, clock: Clock = utc_now):
        self.db_path = Path(db_path)
        self.clock = clock

    @contextmanager
    def _write(self, actor: Actor, action: str, object_id: int | str):
        request_id = actor.request_id if isinstance(actor, Actor) and isinstance(actor.request_id, str) else "production-denied"
        try:
            with transaction(self.db_path, immediate=True) as connection:
                if not isinstance(actor, Actor) or type(actor.admin_id) is not int or not 1 <= actor.admin_id <= 9223372036854775807 or not connection.execute(
                    "SELECT 1 FROM admins WHERE id=? AND role='owner' AND enabled=1", (actor.admin_id,)
                ).fetchone():
                    raise BusinessError("owner_required", "需要有效的经营者身份。", 403)
                yield connection
        except (BusinessError, ValidationError, sqlite3.IntegrityError) as exc:
            if isinstance(exc, ValidationError):
                error = BusinessError("invalid_production", "制作字段无效，请检查字段后重新保存。", 400)
            elif isinstance(exc, sqlite3.IntegrityError):
                error = BusinessError("production_conflict", "制作数据冲突，请重新读取项目。", 409)
            else:
                error = exc
            record_production_denial(self.db_path, actor, action, str(object_id), error, request_id=request_id)
            raise error from None

    @staticmethod
    def _id(value: int) -> None:
        if type(value) is not int or not 1 <= value <= 9223372036854775807:
            raise BusinessError("invalid_identifier", "对象编号无效。", 400)

    @classmethod
    def _row(cls, connection: sqlite3.Connection, project_id: int) -> sqlite3.Row:
        cls._id(project_id)
        row = connection.execute("SELECT * FROM production_projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise BusinessError("project_missing", "制作项目不存在。", 404)
        return row

    @classmethod
    def _editable(cls, connection: sqlite3.Connection, project_id: int, revision: int) -> sqlite3.Row:
        row = cls._row(connection, project_id)
        if type(revision) is not int or revision != row["revision"]:
            raise BusinessError("stale_revision", "页面修订已过期，请复制输入并重新读取项目。", 409)
        if row["status"] == "archived":
            raise BusinessError("project_archived", "项目已归档，无法编辑。", 409)
        if connection.execute("""SELECT 1 FROM production_jobs WHERE project_id=?
            AND state IN ('queued','running')
            AND kind IN ('course_draft_generate','outline_generate','chapters_generate','chapter_regenerate')""", (project_id,)).fetchone():
            raise BusinessError("project_busy", "项目有排队或运行的修改任务，请先停止任务。", 409)
        return row

    @staticmethod
    def _mode(mode: str) -> None:
        if type(mode) is not str or mode not in ("quick", "precise"):
            raise BusinessError("invalid_mode", "请选择快速或精细制作。", 400)

    @staticmethod
    def _brief(connection: sqlite3.Connection, brief: BriefInput) -> BriefInput:
        if not isinstance(brief, BriefInput):
            raise BusinessError("invalid_production", "制作需求字段无效。", 400)
        data = BriefInput.model_validate(brief.model_dump(warnings=False))
        if data.content_kind != "course":
            raise BusinessError("unsupported_content_kind", "本期仅支持课程制作。", 400)
        if data.category_id is not None and not connection.execute(
            "SELECT 1 FROM categories WHERE id=?", (data.category_id,)
        ).fetchone():
            raise BusinessError("category_missing", "请选择已有分类。", 400)
        return data

    @staticmethod
    def _append_brief(connection: sqlite3.Connection, actor: Actor, project_id: int,
                      revision: int, data: BriefInput, now: str) -> int:
        return connection.execute("""INSERT INTO brief_revisions
            (project_id,revision,title,audience,prerequisites,goal,difficulty,chapter_count,
             category_id,content_kind,actor_admin_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (project_id, revision, data.title, data.audience, data.prerequisites, data.goal,
             data.difficulty, data.chapter_count, data.category_id, data.content_kind, actor.admin_id, now)).lastrowid

    @staticmethod
    def _view(connection: sqlite3.Connection, row: sqlite3.Row) -> ProjectView:
        brief = connection.execute("SELECT * FROM brief_revisions WHERE id=? AND project_id=?",
                                   (row["current_brief_revision_id"], row["id"])).fetchone()
        if brief is None:
            raise BusinessError("production_conflict", "项目需求修订缺失，请联系维护者。", 409)
        data = BriefInput(**{key: brief[key] for key in BriefInput.model_fields})
        return ProjectView(row["id"], row["revision"], row["mode"], row["status"], brief["revision"], data)

    def _advance(self, connection: sqlite3.Connection, project_id: int, *, input_changed: bool = True) -> None:
        if input_changed:
            connection.execute("""UPDATE production_projects SET revision=revision+1,updated_at=?,
                current_confirmation_id=NULL,acknowledged_input_hash=NULL WHERE id=?""",
                (to_db_time(self.clock()), project_id))
            connection.execute("""UPDATE production_chapters SET needs_review=1,
                review_reason='input_changed',acknowledged_input_hash=NULL WHERE project_id=? AND active=1""", (project_id,))
        else:
            connection.execute("UPDATE production_projects SET revision=revision+1,updated_at=? WHERE id=?",
                               (to_db_time(self.clock()), project_id))

    @staticmethod
    def _success(connection: sqlite3.Connection, actor: Actor, action: str, project_id: int, revision: int) -> None:
        append_event(connection, AuditEvent(actor.admin_id, "production", str(project_id), action,
                                            "completed", "success", actor.request_id, {"revision": revision}))

    def create(self, actor: Actor, brief: BriefInput, *, mode: str) -> ProjectView:
        with self._write(actor, "production.create", "new") as connection:
            self._mode(mode)
            data = self._brief(connection, brief)
            now = to_db_time(self.clock())
            project_id = connection.execute("""INSERT INTO production_projects
                (actor_admin_id,mode,content_kind,category_id,created_at,updated_at) VALUES (?,?,?,?,?,?)""",
                (actor.admin_id, mode, data.content_kind, data.category_id, now, now)).lastrowid
            brief_id = self._append_brief(connection, actor, project_id, 1, data, now)
            connection.execute("UPDATE production_projects SET current_brief_revision_id=? WHERE id=?", (brief_id, project_id))
            self._success(connection, actor, "production.create", project_id, 1)
            return self._view(connection, self._row(connection, project_id))

    def update(self, actor: Actor, project_id: int, revision: int, brief: BriefInput) -> ProjectView:
        with self._write(actor, "production.update", project_id) as connection:
            row = self._editable(connection, project_id, revision)
            data = self._brief(connection, brief)
            brief_revision = connection.execute("SELECT revision FROM brief_revisions WHERE id=?", (row["current_brief_revision_id"],)).fetchone()[0] + 1
            brief_id = self._append_brief(connection, actor, project_id, brief_revision, data, to_db_time(self.clock()))
            connection.execute("""UPDATE production_projects SET current_brief_revision_id=?,category_id=?,
                content_kind=? WHERE id=?""", (brief_id, data.category_id, data.content_kind, project_id))
            self._advance(connection, project_id)
            self._success(connection, actor, "production.update", project_id, revision + 1)
            return self._view(connection, self._row(connection, project_id))

    def set_mode(self, actor: Actor, project_id: int, revision: int, mode: str) -> ProjectView:
        with self._write(actor, "production.mode", project_id) as connection:
            self._editable(connection, project_id, revision)
            self._mode(mode)
            connection.execute("UPDATE production_projects SET mode=? WHERE id=?", (mode, project_id))
            self._advance(connection, project_id)
            self._success(connection, actor, "production.mode", project_id, revision + 1)
            return self._view(connection, self._row(connection, project_id))

    def archive(self, actor: Actor, project_id: int, revision: int) -> ProjectView:
        with self._write(actor, "production.archive", project_id) as connection:
            self._editable(connection, project_id, revision)
            connection.execute("UPDATE production_projects SET status='archived' WHERE id=?", (project_id,))
            self._advance(connection, project_id, input_changed=False)
            self._success(connection, actor, "production.archive", project_id, revision + 1)
            return self._view(connection, self._row(connection, project_id))

    @classmethod
    def _reference(cls, connection: sqlite3.Connection, project_id: int, reference_id: int) -> sqlite3.Row:
        cls._id(reference_id)
        row = connection.execute("""SELECT r.* FROM project_reference_links l JOIN reference_revisions r
            ON r.id=l.current_revision_id AND r.project_id=l.project_id AND r.reference_id=l.reference_id
            WHERE l.project_id=? AND l.reference_id=? AND l.active=1""", (project_id, reference_id)).fetchone()
        if row is None:
            raise BusinessError("reference_missing", "当前项目的参考资料不存在。", 404)
        return row

    def save_reference(self, actor: Actor, project_id: int, revision: int, reference_id: int | None,
                       data: ReferenceInput) -> ReferenceView:
        with self._write(actor, "production.reference", project_id) as connection:
            self._editable(connection, project_id, revision)
            if not isinstance(data, ReferenceInput):
                raise BusinessError("invalid_production", "参考资料字段无效。", 400)
            data = ReferenceInput.model_validate(data.model_dump(warnings=False))
            old = self._reference(connection, project_id, reference_id) if reference_id is not None else None
            active = connection.execute("""SELECT r.reference_id,r.text FROM project_reference_links l
                JOIN reference_revisions r ON r.id=l.current_revision_id AND r.project_id=l.project_id
                AND r.reference_id=l.reference_id WHERE l.project_id=? AND l.active=1""", (project_id,)).fetchall()
            if (old is None and len(active) >= 10) or sum(len(r["text"]) for r in active if r["reference_id"] != reference_id) + len(data.text) > 20000:
                raise BusinessError("reference_limit", "参考资料最多十条，正文总计最多两万字符。", 400)
            if old is None:
                # Global allocation makes IDs unambiguous at HTTP boundaries;
                # the schema still binds every revision by project and stable ID.
                reference_id = connection.execute("SELECT COALESCE(MAX(reference_id),0)+1 FROM project_reference_links").fetchone()[0]
                position = connection.execute("SELECT COALESCE(MAX(position),0)+1 FROM project_reference_links WHERE project_id=?", (project_id,)).fetchone()[0]
                connection.execute("INSERT INTO project_reference_links (project_id,reference_id,position) VALUES (?,?,?)", (project_id, reference_id, position))
            reference_revision = old["revision"] + 1 if old is not None else 1
            payload = json.dumps(data.model_dump(), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            source_id = connection.execute("""INSERT INTO reference_revisions
                (project_id,reference_id,revision,text,source_note,source_status,content_hash,actor_admin_id,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""", (project_id, reference_id, reference_revision, data.text, data.source_note,
                data.source_status, hashlib.sha256(payload.encode("utf-8")).hexdigest(), actor.admin_id, to_db_time(self.clock()))).lastrowid
            connection.execute("UPDATE project_reference_links SET current_revision_id=? WHERE project_id=? AND reference_id=?", (source_id, project_id, reference_id))
            self._advance(connection, project_id)
            self._success(connection, actor, "production.reference", project_id, revision + 1)
            return ReferenceView(reference_id, reference_revision, data)

    def remove_reference(self, actor: Actor, project_id: int, revision: int, reference_id: int) -> ProjectView:
        with self._write(actor, "production.reference", project_id) as connection:
            self._editable(connection, project_id, revision)
            self._reference(connection, project_id, reference_id)
            connection.execute("UPDATE project_reference_links SET active=0 WHERE project_id=? AND reference_id=?", (project_id, reference_id))
            self._advance(connection, project_id)
            self._success(connection, actor, "production.reference", project_id, revision + 1)
            return self._view(connection, self._row(connection, project_id))

    def get(self, project_id: int) -> ProjectView:
        with closing(open_readonly(self.db_path)) as connection:
            return self._view(connection, self._row(connection, project_id))

    def list_projects(self, *, page: int = 1, archived: bool = False) -> tuple[list[ProjectView], int]:
        if type(page) is not int or not 1 <= page <= 9223372036854775807 // 20 + 1 or type(archived) is not bool:
            raise BusinessError("invalid_page", "分页参数无效。", 400)
        status = "archived" if archived else "draft"
        # Keep count and page in one read transaction without writing anything.
        with closing(open_readonly(self.db_path)) as connection:
            connection.execute("BEGIN")
            total = connection.execute("SELECT COUNT(*) FROM production_projects WHERE status=?", (status,)).fetchone()[0]
            rows = connection.execute("SELECT * FROM production_projects WHERE status=? ORDER BY id DESC LIMIT 20 OFFSET ?",
                                      (status, (page - 1) * 20)).fetchall()
            return [self._view(connection, row) for row in rows], total
