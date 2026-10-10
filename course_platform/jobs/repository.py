"""Database-owned claiming and execution fences; no process-local worker lock."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from secrets import token_urlsafe

from pydantic import ValidationError

from ..audit import AuditEvent, append_event
from ..database import from_db_time, to_db_time, transaction
from ..domain import Actor, BusinessError, Clock, utc_now
from ..production.drafts import DraftService
from ..production.models import (BriefInput, ChapterView, FrozenReference, ModelPublic,
                                 OutlineItem, OutlineView, ProductionInput, ReferenceInput)
from .models import Claim, JobView, TERMINAL_STATES, job_view


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_REASON = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")


class JobRepository:
    def __init__(self, db_path: Path, *, clock: Clock = utc_now):
        self.db_path = Path(db_path)
        self.clock = clock

    @staticmethod
    def _transaction(connection: sqlite3.Connection) -> None:
        if not connection.in_transaction:
            raise BusinessError("job_transaction", "任务写入需要活动事务。", 409)

    @staticmethod
    def _owner(connection: sqlite3.Connection, admin_id: int) -> None:
        if type(admin_id) is not int or not 1 <= admin_id <= 9223372036854775807 or not connection.execute(
            "SELECT 1 FROM admins WHERE id=? AND role='owner' AND enabled=1", (admin_id,)
        ).fetchone():
            raise BusinessError("owner_required", "需要有效的经营者身份。", 403)

    @staticmethod
    def _input(connection: sqlite3.Connection, row: sqlite3.Row) -> ProductionInput | None:
        if row["kind"] == "connection_test":
            if row["input_json"] != "{}" or row["input_hash"] != hashlib.sha256(b"{}").hexdigest():
                raise BusinessError("input_unverified", "冻结输入无法核验，请创建新任务。", 409)
            return None
        # Task 3 verifies every source row and comprehensive hash before deserialization.
        DraftService._job_payload(connection, row)
        raw = json.loads(row["input_json"])
        o = raw["outline"]
        outline = None if o is None else OutlineView(o["id"], o["revision"], o["project_id"],
            tuple(OutlineItem(**item) for item in o["items"]), o["confirmed"])
        return ProductionInput(raw["project_id"], raw["project_revision"], raw["mode"], raw["brief_revision"],
            BriefInput(**raw["brief"]), tuple(FrozenReference(ref["id"], ref["revision"], ReferenceInput(**ref["data"]))
            for ref in raw["references"]), outline, tuple(ChapterView(**c) for c in raw["chapters"]), raw["input_hash"])

    def claim_next(self, worker_id: str) -> Claim | None:
        if type(worker_id) is not str or not _IDENTIFIER.fullmatch(worker_id):
            raise BusinessError("invalid_worker", "工作进程标识无效。", 400)
        try:
            with transaction(self.db_path, immediate=True) as connection:
                if connection.execute("SELECT 1 FROM production_jobs WHERE state='running'").fetchone():
                    return None
                row = connection.execute("SELECT * FROM production_jobs WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
                if row is None:
                    return None
                self._owner(connection, row["actor_admin_id"])
                if row["project_id"] is not None:
                    project = connection.execute("SELECT actor_admin_id FROM production_projects WHERE id=?", (row["project_id"],)).fetchone()
                    if project is None or project["actor_admin_id"] != row["actor_admin_id"]:
                        raise BusinessError("project_forbidden", "无法访问此制作项目。", 403)
                now = self.clock()
                stamp, lease = to_db_time(now), now + timedelta(seconds=60)
                connection.execute("""UPDATE production_jobs SET state='running',revision=revision+1,started_at=?
                    WHERE id=? AND state='queued'""", (stamp, row["id"]))
                attempt_number = connection.execute("SELECT COALESCE(MAX(attempt_number),0)+1 FROM job_attempts WHERE job_id=?",
                    (row["id"],)).fetchone()[0]
                token = token_urlsafe(32)
                attempt_id = connection.execute("""INSERT INTO job_attempts
                    (job_id,attempt_number,attempt_token,worker_id,started_at,heartbeat_at,lease_until)
                    VALUES (?,?,?,?,?,?,?)""", (row["id"], attempt_number, token, worker_id, stamp, stamp, to_db_time(lease))).lastrowid
                connection.execute("""INSERT INTO worker_heartbeats (worker_id,heartbeat_at,started_at,job_id,attempt_id)
                    VALUES (?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET heartbeat_at=excluded.heartbeat_at,
                    job_id=excluded.job_id,attempt_id=excluded.attempt_id""",
                    (worker_id, stamp, stamp, row["id"], attempt_id))
                frozen = self._input(connection, row)
                config = ModelPublic.model_validate(json.loads(row["config_json"]))
                return Claim(row["id"], attempt_id, token, lease, Actor(row["actor_admin_id"], f"job-{row['id']}"), frozen, config)
        except (ValidationError, ValueError, TypeError, KeyError, sqlite3.IntegrityError):
            raise BusinessError("input_unverified", "冻结任务无法核验，请创建新任务。", 409) from None

    def _attempt(self, connection: sqlite3.Connection, claim: Claim, now: datetime) -> tuple[sqlite3.Row, sqlite3.Row]:
        self._transaction(connection)
        if not isinstance(claim, Claim) or type(claim.job_id) is not int or type(claim.attempt_id) is not int or not 1 <= claim.job_id <= 9223372036854775807 or not 1 <= claim.attempt_id <= 9223372036854775807 or not isinstance(claim.actor, Actor) or type(claim.actor.admin_id) is not int or type(claim.attempt_token) is not str:
            raise BusinessError("attempt_expired", "执行授权已失效，无法更新任务。", 409)
        attempt = connection.execute("""SELECT * FROM job_attempts WHERE id=? AND job_id=?
            AND attempt_token=? AND state='running'""", (claim.attempt_id, claim.job_id, claim.attempt_token)).fetchone()
        job = connection.execute("SELECT * FROM production_jobs WHERE id=? AND state='running'",
                                 (claim.job_id,)).fetchone()
        if attempt is None or job is None or job["actor_admin_id"] != claim.actor.admin_id or now >= from_db_time(attempt["lease_until"]):
            raise BusinessError("attempt_expired", "执行授权已失效，无法更新任务。", 409)
        self._owner(connection, job["actor_admin_id"])
        return job, attempt

    def require_attempt_in_tx(self, connection: sqlite3.Connection, claim: Claim, now: datetime) -> JobView:
        job, _ = self._attempt(connection, claim, now)
        return job_view(job)

    def heartbeat(self, claim: Claim) -> None:
        with transaction(self.db_path, immediate=True) as connection:
            now = self.clock()
            _, attempt = self._attempt(connection, claim, now)
            stamp = to_db_time(now)
            connection.execute("UPDATE job_attempts SET heartbeat_at=?,lease_until=? WHERE id=?",
                (stamp, to_db_time(now + timedelta(seconds=60)), attempt["id"]))
            connection.execute("""UPDATE worker_heartbeats SET heartbeat_at=?
                WHERE worker_id=? AND job_id=? AND attempt_id=?""",
                (stamp, attempt["worker_id"], claim.job_id, claim.attempt_id))

    def finish_in_tx(self, connection: sqlite3.Connection, claim: Claim, state: str,
                     reason_code: str | None) -> JobView:
        job, attempt = self._attempt(connection, claim, self.clock())
        if type(state) is not str or state not in TERMINAL_STATES or (reason_code is not None and (
            type(reason_code) is not str or not _REASON.fullmatch(reason_code))):
            raise BusinessError("invalid_job_state", "任务终结状态或原因无效。", 400)
        if state == "succeeded" and (job["cancel_requested"] or connection.execute(
            "SELECT 1 FROM job_steps WHERE job_id=? AND state!='succeeded'", (job["id"],)
        ).fetchone()):
            raise BusinessError("invalid_job_state", "任务尚未全部成功，无法标为完成。", 409)
        stamp = to_db_time(self.clock())
        connection.execute("""UPDATE job_attempts SET state=?,finished_at=? WHERE id=?""",
                           (state, stamp, attempt["id"]))
        connection.execute("""UPDATE production_jobs SET state=?,reason_code=?,revision=revision+1,
            finished_at=? WHERE id=?""", (state, reason_code, stamp, job["id"]))
        connection.execute("""UPDATE worker_heartbeats SET job_id=NULL,attempt_id=NULL WHERE worker_id=?
            AND job_id=? AND attempt_id=?""", (attempt["worker_id"], job["id"], attempt["id"]))
        result = job_view(connection.execute("SELECT * FROM production_jobs WHERE id=?", (job["id"],)).fetchone())
        if state in ("cancelled", "interrupted"):
            action = "job.cancel" if state == "cancelled" else "job.interrupt"
            append_event(connection, AuditEvent(job["actor_admin_id"], "job", str(job["id"]), action,
                "completed", "success", f"job-{job['id']}", {"revision": result.revision, "job_state": state}))
        return result

    def mark_interrupted(self, *, worker_id: str | None = None) -> int:
        if worker_id is not None and (type(worker_id) is not str or not _IDENTIFIER.fullmatch(worker_id)):
            raise BusinessError("invalid_worker", "工作进程标识无效。", 400)
        with transaction(self.db_path, immediate=True) as connection:
            now = self.clock()
            rows = connection.execute("""SELECT a.*,j.actor_admin_id,j.revision AS job_revision
                FROM job_attempts a JOIN production_jobs j ON j.id=a.job_id
                WHERE a.state='running' AND j.state='running' AND (? IS NULL OR a.worker_id=?)""",
                (worker_id, worker_id)).fetchall()
            count = 0
            for attempt in rows:
                if now - from_db_time(attempt["heartbeat_at"]) < timedelta(seconds=120):
                    continue
                stamp = to_db_time(now)
                connection.execute("UPDATE job_attempts SET state='interrupted',finished_at=? WHERE id=?",
                    (stamp, attempt["id"]))
                connection.execute("""UPDATE production_jobs SET state='interrupted',reason_code='worker_lost',
                    revision=revision+1,finished_at=? WHERE id=?""", (stamp, attempt["job_id"]))
                connection.execute("""UPDATE worker_heartbeats SET job_id=NULL,attempt_id=NULL WHERE worker_id=?
                    AND job_id=? AND attempt_id=?""", (attempt["worker_id"], attempt["job_id"], attempt["id"]))
                append_event(connection, AuditEvent(attempt["actor_admin_id"], "job", str(attempt["job_id"]),
                    "job.interrupt", "worker_lost", "success", f"job-{attempt['job_id']}",
                    {"revision": attempt["job_revision"] + 1, "job_state": "interrupted"}))
                count += 1
            return count
