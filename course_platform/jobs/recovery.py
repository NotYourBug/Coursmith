"""Explicit cancellation, append-only usage reconciliation and fresh-job recovery."""

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
from ..production.drafts import DraftService
from ..production.events import record_production_denial
from ..production.llm import Usage
from ..production.models import ModelPublic
from ..production.projects import ProjectService
from .budget import TokenBudget
from .handlers import release_unused_pool_in_tx
from .models import JobRequest, JobView, RecoveryPreview, canonical_json, job_view
from .repository import JobRepository
from .service import JobService, _KEY
from .worker import maintain


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _verified(condition: bool) -> None:
    if not condition:
        raise BusinessError("input_unverified", "原任务输入或结果无法核验，请创建新任务。", 409)


class RecoveryService:
    def __init__(self, db_path: Path, jobs: JobService, drafts: DraftService,
                 budget: TokenBudget, *, clock: Clock = utc_now):
        self.db_path = Path(db_path)
        if any(self.db_path.resolve() != service.db_path.resolve() for service in (jobs, drafts, budget)):
            raise BusinessError("job_database", "恢复、任务、草稿与额度必须使用同一数据库。", 409)
        self.jobs, self.drafts, self.budget, self.clock = jobs, drafts, budget, clock
        self.repository = JobRepository(self.db_path, clock=clock)

    @staticmethod
    def _row(connection: sqlite3.Connection, job_id: int) -> sqlite3.Row:
        ProjectService._id(job_id)
        row = connection.execute("SELECT * FROM production_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise BusinessError("job_missing", "任务不存在。", 404)
        return row

    def _authorized(self, connection, actor, job_id, revision):
        self.jobs._owner(connection, actor)
        row = self._row(connection, job_id)
        if row["actor_admin_id"] != actor.admin_id:
            raise BusinessError("job_forbidden", "无法操作此任务。", 403)
        if row["project_id"] is not None:
            project = ProjectService._row(connection, row["project_id"])
            if project["actor_admin_id"] != actor.admin_id:
                raise BusinessError("project_forbidden", "无法操作此制作项目。", 403)
        if type(revision) is not int or revision != row["revision"]:
            raise BusinessError("stale_revision", "任务修订已过期，请重新读取后明确操作。", 409)
        return row

    @contextmanager
    def _write(self, actor, action, job_id):
        request_id = actor.request_id if isinstance(actor, Actor) and isinstance(actor.request_id, str) else "recovery-denied"
        try:
            with transaction(self.db_path, immediate=True) as connection:
                yield connection
        except (BusinessError, ValidationError, sqlite3.IntegrityError, ValueError, TypeError, KeyError) as exc:
            if isinstance(exc, BusinessError):
                error = exc
            else:
                error = BusinessError("recovery_conflict", "恢复数据无效或已变化，请重新读取。", 409)
            record_production_denial(self.db_path, actor, action, str(job_id), error, request_id=request_id)
            raise error from None

    def _event(self, connection, actor, action, row):
        changes = {"revision": row["revision"]}
        if action != "job.reconcile":
            changes["job_state"] = row["state"]
        append_event(connection, AuditEvent(actor.admin_id, "job", str(row["id"]), action,
            "completed", "success", actor.request_id, changes))

    def cancel(self, actor: Actor, job_id: int, revision: int) -> JobView:
        # Authorize before the explicit two-phase maintenance coordinator. Recheck
        # afterwards: interruption may legitimately make this submitted view stale.
        with self._write(actor, "job.cancel", job_id) as connection:
            self._authorized(connection, actor, job_id, revision)
        maintain(self.repository, self.budget)
        with self._write(actor, "job.cancel", job_id) as connection:
            row = self._authorized(connection, actor, job_id, revision)
            if row["state"] == "cancelled" or (row["state"] == "running" and row["cancel_requested"]):
                return job_view(row)
            if row["state"] not in ("queued", "running"):
                raise BusinessError("invalid_job_state", "任务已停止，请读取结果并选择恢复。", 409)
            stamp = to_db_time(self.clock())
            if row["state"] == "queued":
                connection.execute("""UPDATE production_jobs SET state='cancelled',cancel_requested=1,
                    reason_code='job_cancelled',finished_at=?,revision=revision+1 WHERE id=?""", (stamp, job_id))
                release_unused_pool_in_tx(connection, job_id, stamp)
            else:
                connection.execute("UPDATE production_jobs SET cancel_requested=1,revision=revision+1 WHERE id=?", (job_id,))
            key = f"cancel:{job_id}:{revision}"
            connection.execute("""INSERT INTO job_requests
                (actor_admin_id,action,idempotency_key,request_digest,job_id,created_at)
                VALUES (?,'cancel',?,?,?,?)""", (actor.admin_id, key, _digest({"job_id": job_id, "revision": revision}), job_id, stamp))
            current = self._row(connection, job_id)
            self._event(connection, actor, "job.cancel", current)
            return job_view(current)

    def _progress_in_tx(self, connection, job):
        """Prove the entire original goal and replay only exact succeeded OWN outputs."""
        original = self.repository._input(connection, job)
        payload = self.drafts._source_payload(original) if original else None
        selected = json.loads(job["chapter_ids_json"])
        _verified(type(selected) is list and len(selected) == len(set(selected)))
        kind = job["kind"]
        if kind in ("chapters_generate", "chapter_regenerate"):
            available = [c.id for c in original.chapters]
            _verified(bool(selected) and all(cid in available for cid in selected)
                      and selected == [cid for cid in available if cid in selected])
            _verified(kind != "chapter_regenerate" or len(selected) == 1)
            plan = [("chapter", cid) for cid in selected]
            if kind == "chapters_generate":
                plan.append(("validate", None))
        elif kind in ("course_draft_generate", "outline_generate"):
            _verified(not selected)
            plan = [("outline", None)]
        else:
            _verified(not selected)
            plan = [("validate" if kind == "draft_validate" else "connection_test", None)]
        attempts = connection.execute("SELECT * FROM job_attempts WHERE job_id=? ORDER BY attempt_number", (job["id"],)).fetchall()
        _verified(len(attempts) <= 1)
        steps = connection.execute("SELECT * FROM job_steps WHERE job_id=? ORDER BY step_number", (job["id"],)).fetchall()
        completed = []
        succeeded = set()
        candidate_outline = False
        failed_seen = False
        for number, step in enumerate(steps, 1):
            _verified(len(attempts) == 1 and step["attempt_id"] == attempts[0]["id"]
                and step["project_id"] == job["project_id"] and step["step_number"] == number
                and number <= len(plan) and (step["kind"], step["chapter_id"]) == plan[number - 1]
                and step["input_hash"] == (_digest(payload) if payload else job["input_hash"]))
            if step["state"] != "succeeded":
                failed_seen = True
                continue
            _verified(not failed_seen)
            succeeded.add((step["kind"], step["chapter_id"]))
            if step["kind"] in ("chapter", "outline"):
                source = self.drafts._step_source(connection, step)
                _verified(source["actor_admin_id"] == job["actor_admin_id"])
                if step["kind"] == "chapter":
                    matching = [c for c in payload["chapters"] if c["id"] == step["chapter_id"]]
                    _verified(len(matching) == 1)
                    if step["candidate_id"] is None:
                        _verified(matching[0]["body_revision"] is None)
                        matching[0].update(body_revision=source["revision"], markdown=source["markdown"])
                    else:
                        candidate = connection.execute("SELECT * FROM production_candidates WHERE id=?", (step["candidate_id"],)).fetchone()
                        _verified(candidate["base_body_revision_id"] == self.drafts._base_id(
                            connection, job["project_id"], step["chapter_id"], matching[0]["body_revision"]))
                    completed.append(step["chapter_id"])
                elif step["candidate_id"] is None:
                    _verified(payload["outline"] is None and payload["mode"] == "quick")
                    view = self.drafts._outline_view(connection, ProjectService._row(connection, job["project_id"]), source["id"])
                    payload["outline"] = self.drafts._outline_payload(view)
                    payload["chapters"] = [{"id": item.chapter_id, "body_revision": None, "markdown": ""} for item in view.items]
                    if kind == "course_draft_generate":
                        plan.extend(("chapter", c["id"]) for c in payload["chapters"])
                        plan.append(("validate", None))
                elif kind == "course_draft_generate" and original.outline is None:
                    candidate_outline = True
            elif step["kind"] == "validate":
                check = connection.execute("SELECT * FROM draft_checks WHERE id=? AND project_id=?", (step["check_id"], job["project_id"])).fetchone()
                _verified(check is not None and check["input_hash"] == step["input_hash"])
        current = self.drafts._freeze_in_tx(connection, job["project_id"]) if original else None
        unchanged = (_digest(payload) == current.input_hash) if current else True
        pending = tuple(cid for action, cid in plan if action == "chapter" and (action, cid) not in succeeded)
        unknown = tuple(row["id"] for row in connection.execute(
            "SELECT id FROM model_calls WHERE job_id=? AND status='unknown' ORDER BY id", (job["id"],)))
        return RecoveryPreview(unchanged, tuple(completed), pending, unknown), plan, succeeded, candidate_outline, current

    def recovery_preview(self, job_id: int) -> RecoveryPreview:
        """Trusted read API; HTTP callers must authorize first. No maintenance/network."""
        with closing(open_readonly(self.db_path)) as connection:
            connection.execute("BEGIN")
            try:
                return self._progress_in_tx(connection, self._row(connection, job_id))[0]
            except (ValueError, TypeError, KeyError, ValidationError):
                raise BusinessError("input_unverified", "原任务输入或结果无法核验，请创建新任务。", 409) from None

    def reconcile(self, actor: Actor, job_id: int, call_id: int, revision: int, *,
                  usage: Usage | None, accept_retry_risk: bool) -> None:
        with self._write(actor, "job.reconcile", job_id) as connection:
            job = self._authorized(connection, actor, job_id, revision)
            ProjectService._id(call_id)
            if job["state"] not in ("failed", "cancelled", "interrupted"):
                raise BusinessError("invalid_job_state", "请等待任务停止后核对未知调用。", 409)
            call = connection.execute("SELECT * FROM model_calls WHERE id=? AND job_id=?", (call_id, job_id)).fetchone()
            if call is None:
                raise BusinessError("model_call_missing", "此调用不属于当前任务。", 404)
            if call["status"] != "unknown":
                raise BusinessError("settlement_conflict", "只有原未知调用可以核对，不能覆盖已知账目。", 409)
            if type(accept_retry_risk) is not bool or (usage is not None and not isinstance(usage, Usage)):
                raise BusinessError("invalid_model_settlement", "请提供有效用量或明确接受重试风险。", 400)
            if usage is None and not accept_retry_risk:
                raise BusinessError("reconciliation_required", "请填写已核对用量或明确接受再次调用风险。", 409)
            if usage is not None:
                usage = Usage(usage.input_tokens, usage.output_tokens)
                self.budget.settle_in_tx(connection, call_id=call_id, usage=usage, status="successful")
            data = {"job_id": job_id, "call_id": call_id, "revision": revision,
                    "usage": None if usage is None else [usage.input_tokens, usage.output_tokens],
                    "accept_retry_risk": accept_retry_risk}
            connection.execute("""INSERT INTO job_requests
                (actor_admin_id,action,idempotency_key,request_digest,job_id,call_id,input_tokens,output_tokens,accept_retry_risk,created_at)
                VALUES (?,'reconcile',?,?,?,?,?,?,?,?)""", (actor.admin_id, f"reconcile:{job_id}:{call_id}:{revision}",
                _digest(data), job_id, call_id, usage.input_tokens if usage else None,
                usage.output_tokens if usage else None, int(accept_retry_risk), to_db_time(self.clock())))
            connection.execute("UPDATE production_jobs SET revision=revision+1 WHERE id=?", (job_id,))
            self._event(connection, actor, "job.reconcile", self._row(connection, job_id))

    def resume(self, actor: Actor, job_id: int, revision: int, config: ModelPublic, *,
               idempotency_key: str, reserved_tokens: int, confirm: bool) -> JobView:
        with self._write(actor, "job.resume", job_id) as connection:
            job = self._authorized(connection, actor, job_id, revision)
            if type(confirm) is not bool or not confirm:
                raise BusinessError("recovery_confirmation_required", "请查看恢复摘要和新预留额度后明确确认。", 409)
            if type(idempotency_key) is not str or not _KEY.fullmatch(idempotency_key):
                raise BusinessError("invalid_idempotency_key", "恢复请求标识无效。", 400)
            if type(reserved_tokens) is not int or not 0 <= reserved_tokens <= 9223372036854775807:
                raise BusinessError("invalid_reservation", "任务预留额度无效。", 400)
            if not isinstance(config, ModelPublic):
                raise BusinessError("invalid_model_config", "模型配置无效。", 400)
            config = ModelPublic.model_validate(config.model_dump(warnings=False))
            digest = _digest({"actor_admin_id": actor.admin_id, "parent_job_id": job_id,
                "revision": revision, "confirm": confirm, "config": config.model_dump(warnings=False)})
            prior = connection.execute("SELECT * FROM job_requests WHERE actor_admin_id=? AND action='resume' AND idempotency_key=?",
                                       (actor.admin_id, idempotency_key)).fetchone()
            if prior is not None:
                if prior["request_digest"] != digest:
                    raise BusinessError("idempotency_conflict", "请求标识已用于不同恢复，请使用新标识。", 409)
                child = self._row(connection, prior["job_id"])
                _verified(child["parent_job_id"] == job_id and child["actor_admin_id"] == actor.admin_id
                          and child["project_id"] == job["project_id"])
                return job_view(child)
            # A child owns the continuation even after it stops. Branching from
            # its ancestor would omit the child's successful work and unknown calls.
            if connection.execute("SELECT 1 FROM production_jobs WHERE parent_job_id=?", (job_id,)).fetchone():
                raise BusinessError("recovery_superseded", "原任务已有恢复任务，请从关联恢复任务继续处理。", 409)
            if job["state"] == "succeeded":
                raise BusinessError("recovery_not_needed", "原任务已完成，无需恢复。", 409)
            if job["state"] not in ("failed", "cancelled", "interrupted"):
                raise BusinessError("invalid_job_state", "请等待任务停止后恢复。", 409)
            if canonical_json(config.model_dump(warnings=False)) != job["config_json"]:
                raise BusinessError("model_config_changed", "执行条件已变化，请创建新的制作任务。", 409)
            preview, plan, succeeded, candidate_outline, current = self._progress_in_tx(connection, job)
            if not preview.unchanged_input:
                raise BusinessError("input_changed", "制作输入已变化，请重新提交并确认当前精细大纲。", 409)
            if candidate_outline:
                raise BusinessError("recovery_candidate_pending", "已生成的大纲为候选，请人工采用并提交新的制作任务。", 409)
            if connection.execute("SELECT 1 FROM model_calls WHERE job_id=? AND status='sent'", (job_id,)).fetchone():
                raise BusinessError("model_call_pending", "请先明确检查中断状态并核对调用。", 409)
            for call_id in preview.unknown_call_ids:
                call = connection.execute("SELECT * FROM model_calls WHERE id=?", (call_id,)).fetchone()
                reserve = connection.execute("SELECT * FROM token_reservations WHERE call_id=?", (call_id,)).fetchone()
                resolved = call["input_tokens"] is not None and reserve is not None and reserve["used_tokens"] == call["input_tokens"] + call["output_tokens"]
                consent = connection.execute("SELECT 1 FROM job_requests WHERE actor_admin_id=? AND action='reconcile' AND job_id=? AND call_id=? AND accept_retry_risk=1",
                    (actor.admin_id, job_id, call_id)).fetchone()
                if not resolved and not consent:
                    raise BusinessError("model_usage_unknown", "请先核对全部未知调用或明确记录再次调用风险。", 409)
            remaining = [step for step in plan if step not in succeeded]
            if not remaining:
                raise BusinessError("recovery_not_needed", "原任务的工作已完成，无需恢复。", 409)
            if preview.pending_chapter_ids:
                kind = "chapter_regenerate" if job["kind"] == "chapter_regenerate" else "chapters_generate"
                selected = preview.pending_chapter_ids
            elif remaining[0][0] == "validate":
                kind, selected = "draft_validate", ()
            else:
                kind, selected = job["kind"], ()
            request = JobRequest(kind=kind, project_id=job["project_id"],
                project_revision=current.project_revision if current else None,
                chapter_ids=selected, parent_job_id=job_id)
            child = self.jobs._submit_in_tx(connection, actor, request, config,
                idempotency_key="resume:" + _digest({"key": idempotency_key, "payload": digest}),
                reserved_tokens=reserved_tokens)
            connection.execute("""INSERT INTO job_requests
                (actor_admin_id,action,idempotency_key,request_digest,job_id,created_at)
                VALUES (?,'resume',?,?,?,?)""", (actor.admin_id, idempotency_key, digest, child.id, to_db_time(self.clock())))
            self._event(connection, actor, "job.resume", self._row(connection, child.id))
            return child
