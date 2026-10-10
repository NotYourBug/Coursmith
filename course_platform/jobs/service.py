"""Atomic job submission with request-scoped idempotency and frozen source inputs."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from pydantic import ValidationError

from ..audit import AuditEvent, append_event
from ..database import open_readonly, to_db_time, transaction
from ..domain import Actor, BusinessError, Clock, utc_now
from ..production.drafts import DraftService
from ..production.events import record_production_denial
from ..production.models import ModelPublic
from ..production.projects import ProjectService
from .models import JobRequest, JobView, MODIFYING_KINDS, canonical_json, frozen_payload, job_view


_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class JobService:
    def __init__(self, db_path: Path, drafts: DraftService, *, clock: Clock = utc_now):
        self.db_path = Path(db_path)
        if self.db_path.resolve() != drafts.db_path.resolve():
            raise BusinessError("job_database", "任务与草稿必须使用同一数据库。", 409)
        self.drafts = drafts
        self.clock = clock

    @staticmethod
    def _owner(connection: sqlite3.Connection, actor: Actor) -> None:
        if not isinstance(actor, Actor) or type(actor.admin_id) is not int or not 1 <= actor.admin_id <= 9223372036854775807 or not connection.execute(
            "SELECT 1 FROM admins WHERE id=? AND role='owner' AND enabled=1", (actor.admin_id,)
        ).fetchone():
            raise BusinessError("owner_required", "需要有效的经营者身份。", 403)

    def submit(self, actor: Actor, request: JobRequest, config: ModelPublic, *,
               idempotency_key: str, reserved_tokens: int) -> JobView:
        """reserved_tokens is trusted server input; Task 6 owns budget computation."""
        object_id = getattr(request, "project_id", "unknown") if isinstance(request, JobRequest) else "unknown"
        request_id = actor.request_id if isinstance(actor, Actor) and isinstance(actor.request_id, str) else "job-denied"
        try:
            with transaction(self.db_path, immediate=True) as connection:
                self._owner(connection, actor)
                if not isinstance(request, JobRequest) or not isinstance(config, ModelPublic):
                    raise BusinessError("invalid_job", "任务字段无效，请重新提交。", 400)
                request = JobRequest.model_validate(request.model_dump(warnings=False))
                config = ModelPublic.model_validate(config.model_dump(warnings=False))
                if type(idempotency_key) is not str or not _KEY.fullmatch(idempotency_key):
                    raise BusinessError("invalid_idempotency_key", "任务请求标识无效，请重新提交。", 400)
                if type(reserved_tokens) is not int or not 0 <= reserved_tokens <= 9223372036854775807:
                    raise BusinessError("invalid_reservation", "任务预留额度无效，请联系维护者。", 400)
                data = request.model_dump(warnings=False)
                # Selection is a set; source/execution order remains the frozen outline's order.
                data["chapter_ids"] = sorted(request.chapter_ids)
                digest = hashlib.sha256(canonical_json({
                    "actor_admin_id": actor.admin_id, "request": data,
                    "config": config.model_dump(warnings=False), "reserved_tokens": reserved_tokens,
                }).encode("utf-8")).hexdigest()
                prior = connection.execute("""SELECT j.*,r.request_digest AS idempotent_digest
                    FROM job_requests r JOIN production_jobs j ON j.id=r.job_id
                    WHERE r.actor_admin_id=? AND r.action='submit' AND r.idempotency_key=?""",
                    (actor.admin_id, idempotency_key)).fetchone()
                if prior is not None:
                    if prior["idempotent_digest"] != digest or prior["request_digest"] != digest or prior["actor_admin_id"] != actor.admin_id or prior["project_id"] != request.project_id:
                        raise BusinessError("idempotency_conflict", "请求标识已用于不同任务，请使用新的请求标识。", 409)
                    if prior["project_id"] is not None:
                        project = ProjectService._row(connection, prior["project_id"])
                        if project["actor_admin_id"] != actor.admin_id:
                            raise BusinessError("project_forbidden", "无法访问此制作项目。", 403)
                    return job_view(prior)

                frozen = None
                selected = request.chapter_ids
                if request.project_id is not None:
                    project = ProjectService._row(connection, request.project_id)
                    if project["actor_admin_id"] != actor.admin_id:
                        raise BusinessError("project_forbidden", "无法访问此制作项目。", 403)
                    if project["revision"] != request.project_revision:
                        raise BusinessError("stale_revision", "项目修订已过期，请重新读取后提交。", 409)
                    if project["status"] == "archived":
                        raise BusinessError("project_archived", "项目已归档，无法提交新任务。", 409)
                    if request.kind in MODIFYING_KINDS and connection.execute("""SELECT 1 FROM production_jobs
                        WHERE project_id=? AND state IN ('queued','running')
                        AND kind IN ('course_draft_generate','outline_generate','chapters_generate','chapter_regenerate')""",
                        (project["id"],)).fetchone():
                        raise BusinessError("project_busy", "项目已有修改任务，请等待停止后重新提交。", 409)
                    frozen = self.drafts._freeze_in_tx(connection, request.project_id)
                    if request.kind == "course_draft_generate" and frozen.mode != "quick":
                        raise BusinessError("invalid_job_mode", "精细模式请先生成并确认大纲。", 409)
                    if request.kind in ("chapters_generate", "chapter_regenerate"):
                        if frozen.outline is None:
                            raise BusinessError("outline_missing", "请先创建大纲。", 409)
                        ids = tuple(c.id for c in frozen.chapters)
                        if not set(selected) <= set(ids):
                            raise BusinessError("chapter_missing", "所选章节不属于当前项目大纲。", 404)
                        selected = tuple(cid for cid in ids if cid in selected) if selected else ids
                    if request.kind == "chapters_generate" and frozen.mode == "precise" and not frozen.outline.confirmed:
                        raise BusinessError("outline_unconfirmed", "请确认当前大纲后生成正文。", 409)
                if request.parent_job_id is not None:
                    parent = connection.execute("SELECT * FROM production_jobs WHERE id=?", (request.parent_job_id,)).fetchone()
                    if parent is None or parent["project_id"] != request.project_id or parent["actor_admin_id"] != actor.admin_id or parent["state"] in ("queued", "running"):
                        raise BusinessError("parent_job_conflict", "关联任务无效，请重新读取。", 409)

                payload = frozen_payload(frozen) if frozen else {}
                input_json = canonical_json(payload)
                input_hash = frozen.input_hash if frozen else hashlib.sha256(input_json.encode("utf-8")).hexdigest()
                now = to_db_time(self.clock())
                job_id = connection.execute("""INSERT INTO production_jobs
                    (project_id,actor_admin_id,kind,project_revision,parent_job_id,input_hash,input_json,
                     config_json,chapter_ids_json,request_digest,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (request.project_id, actor.admin_id, request.kind, request.project_revision, request.parent_job_id,
                     input_hash, input_json, canonical_json(config.model_dump(warnings=False)),
                     canonical_json(selected), digest, now)).lastrowid
                # No retry, estimation, settlement or limit policy is chosen in this task.
                connection.execute("""INSERT INTO token_reservations (job_id,reserved_tokens,created_at)
                    VALUES (?,?,?)""", (job_id, reserved_tokens, now))
                connection.execute("""INSERT INTO job_requests
                    (actor_admin_id,action,idempotency_key,request_digest,job_id,created_at)
                    VALUES (?,'submit',?,?,?,?)""", (actor.admin_id, idempotency_key, digest, job_id, now))
                append_event(connection, AuditEvent(actor.admin_id, "job", str(job_id), "job.submit",
                    "completed", "success", actor.request_id, {"revision": 1, "job_state": "queued"}))
                return job_view(connection.execute("SELECT * FROM production_jobs WHERE id=?", (job_id,)).fetchone())
        except (BusinessError, ValidationError, sqlite3.IntegrityError) as exc:
            if isinstance(exc, ValidationError):
                error = BusinessError("invalid_job", "任务字段无效，请检查后重新提交。", 400)
            elif isinstance(exc, sqlite3.IntegrityError):
                error = BusinessError("job_conflict", "任务数据冲突，请重新读取。", 409)
            else:
                error = exc
            record_production_denial(self.db_path, actor, "job.submit", str(object_id), error, request_id=request_id)
            raise error from None

    def get(self, job_id: int) -> JobView:
        ProjectService._id(job_id)
        with closing(open_readonly(self.db_path)) as connection:
            row = connection.execute("SELECT * FROM production_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise BusinessError("job_missing", "任务不存在。", 404)
            return job_view(row)

    def list_jobs(self, *, page: int = 1) -> tuple[list[JobView], int]:
        ProjectService._id(page)
        # Avoid overflowing SQLite's signed integer offset.
        if page > 9223372036854775807 // 20:
            raise BusinessError("invalid_identifier", "页码无效。", 400)
        with closing(open_readonly(self.db_path)) as connection:
            connection.execute("BEGIN")
            total = connection.execute("SELECT COUNT(*) FROM production_jobs").fetchone()[0]
            rows = connection.execute("SELECT * FROM production_jobs ORDER BY id DESC LIMIT 20 OFFSET ?",
                                      ((page - 1) * 20,)).fetchall()
            return [job_view(row) for row in rows], total
