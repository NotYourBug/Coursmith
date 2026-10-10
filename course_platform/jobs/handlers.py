"""Fixed, fenced generation handlers. Network calls never own a database lock."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from asyncio import sleep, timeout
from dataclasses import asdict, replace

from ..audit import AuditEvent, append_event
from ..database import to_db_time, transaction
from ..domain import BusinessError
from ..production.checks import validate_draft
from ..production.config import ProductionSettings
from ..production.drafts import DraftService
from ..production.llm import ModelClient, ModelFailure, ModelResult, Usage, parse_outline, retry_delay
from ..production.models import ChapterView, ProductionInput
from ..production.prompts import build_chapter_request, build_connection_request, build_outline_request
from .budget import TokenBudget
from .models import Claim, canonical_json
from .repository import JobRepository


def release_unused_pool_in_tx(connection: sqlite3.Connection, job_id: int, stamp: str) -> None:
    """Terminal-only release of unused planned capacity; never a call refund."""
    if not connection.in_transaction or not connection.execute(
        "SELECT 1 FROM production_jobs WHERE id=? AND state IN ('succeeded','failed','cancelled','interrupted')",
        (job_id,),
    ).fetchone():
        raise BusinessError("invalid_job_state", "任务未停止，不能释放计划额度。", 409)
    connection.execute("""UPDATE token_reservations SET released_tokens=reserved_tokens,
        state='released',settled_at=COALESCE(settled_at,?) WHERE job_id=? AND call_id IS NULL""", (stamp, job_id))


class Handlers:
    def __init__(self, claim, *, drafts, repository, budget, model, config):
        self.claim, self.drafts, self.repository = claim, drafts, repository
        self.budget, self.model, self.config = budget, model, config
        self.frozen = claim.input
        self.kind = None
        self.selected = ()
        self.expected = []
        self.step_id = None
        self.call_id = None
        self.result = None

    @classmethod
    async def execute(cls, claim: Claim, *, drafts: DraftService, repository: JobRepository,
                      budget: TokenBudget, model: ModelClient, config: ProductionSettings) -> None:
        execution = cls(claim, drafts=drafts, repository=repository, budget=budget, model=model, config=config)
        try:
            await asyncio.to_thread(execution._prepare)
            await execution._run()
        except asyncio.CancelledError:
            await asyncio.shield(asyncio.to_thread(execution._isolate, "worker_stopped"))
            raise
        except (BusinessError, ModelFailure, sqlite3.Error, TimeoutError, ValueError, TypeError, UnicodeError):
            # Safe codes only; private text and SDK exceptions never reach logs.
            import sys

            error = sys.exception()
            code = error.code if isinstance(error, (BusinessError, ModelFailure)) else (
                "response_unknown" if isinstance(error, TimeoutError) else "storage_failure")
            await asyncio.to_thread(execution._fail, code)

    def _fence(self, connection, *, allow_cancel=False):
        self.repository.require_attempt_in_tx(connection, self.claim, self.repository.clock())
        job = connection.execute("SELECT * FROM production_jobs WHERE id=?", (self.claim.job_id,)).fetchone()
        if job["cancel_requested"] and not allow_cancel:
            raise BusinessError("job_cancelled", "任务已请求取消。", 409)
        return job

    def _current_matches(self, connection):
        if self.frozen is None:
            return
        current = self.drafts._freeze_in_tx(connection, self.frozen.project_id)
        if current.input_hash != self.frozen.input_hash:
            raise BusinessError("input_changed", "制作输入已变化，请创建新任务。", 409)
        if self.kind == "chapters_generate" and self.frozen.mode == "precise" and (
            current.outline is None or not current.outline.confirmed
        ):
            raise BusinessError("outline_unconfirmed", "请确认当前大纲后生成正文。", 409)

    def _prepare(self):
        with transaction(self.repository.db_path, immediate=True) as connection:
            job = self._fence(connection)
            original = self.repository._input(connection, job)
            if original != self.claim.input:
                raise BusinessError("input_unverified", "执行输入不符合原任务。", 409)
            self.frozen = original
            self.kind = job["kind"]
            self.selected = tuple(json.loads(job["chapter_ids_json"]))
            self._current_matches(connection)
            if self.kind != "draft_validate":
                self.config.require_execution(self.claim.config)
            if job["config_json"] != canonical_json(self.claim.config.model_dump(warnings=False)):
                raise BusinessError("model_config_changed", "执行条件不符合原任务。", 409)
            if connection.execute("SELECT 1 FROM job_steps WHERE attempt_id=?", (self.claim.attempt_id,)).fetchone():
                raise BusinessError("job_steps_incomplete", "不能重放已经开始的任务步骤。", 409)
            if self.kind in ("outline_generate", "course_draft_generate"):
                self.expected = [("outline", None)]
            elif self.kind in ("chapters_generate", "chapter_regenerate"):
                self.expected = [("chapter", cid) for cid in self.selected]
                if self.kind == "chapters_generate":
                    self.expected.append(("validate", None))
            elif self.kind == "draft_validate":
                self.expected = [("validate", None)]
            elif self.kind == "connection_test":
                self.expected = [("connection_test", None)]
            else:
                raise BusinessError("invalid_job", "任务类型无效。", 409)

    def _new_step(self, kind, chapter_id):
        with transaction(self.repository.db_path, immediate=True) as connection:
            job = self._fence(connection)
            self._current_matches(connection)
            number = connection.execute("SELECT COUNT(*)+1 FROM job_steps WHERE attempt_id=?",
                                        (self.claim.attempt_id,)).fetchone()[0]
            if number > len(self.expected) or self.expected[number - 1] != (kind, chapter_id):
                raise BusinessError("job_steps_incomplete", "步骤不符合任务计划。", 409)
            self.step_id = connection.execute("""INSERT INTO job_steps
                (job_id,attempt_id,project_id,step_number,kind,chapter_id,input_hash,state,started_at)
                VALUES (?,?,?,?,?,?,?,'running',?)""", (self.claim.job_id, self.claim.attempt_id,
                self.frozen.project_id if self.frozen else None, number, kind, chapter_id,
                self.frozen.input_hash if self.frozen else job["input_hash"], to_db_time(self.repository.clock()))).lastrowid

    def _begin(self, request):
        with transaction(self.repository.db_path, immediate=True) as connection:
            self._fence(connection)
            self._current_matches(connection)
            # The ledger sees this exact own-output-derived full freeze.
            return self.budget.begin_call_in_tx(connection, replace(self.claim, input=self.frozen), request)

    def _rejected(self, error):
        with transaction(self.repository.db_path, immediate=True) as connection:
            self.budget.settle_in_tx(connection, call_id=self.call_id, usage=Usage(0, 0), status="rejected")
            connection.execute("UPDATE model_calls SET error_code=? WHERE id=?", (error.code, self.call_id))

    async def _call(self, request):
        self.result = None
        for retry in range(3):
            self.call_id = await asyncio.to_thread(self._begin, request)
            try:
                async with timeout(self.config.timeout_seconds):
                    self.result = await self.model.generate(request, deadline_seconds=self.config.timeout_seconds)
                self._valid_result(self.result)
                return
            except ModelFailure as error:
                # ONLY the actual adapter's never-accepted proof is a zero-use receipt.
                if error.code == "not_sent" and error.accepted is False:
                    await asyncio.to_thread(self._rejected, error)
                    delay = None if self.kind == "connection_test" else retry_delay(error, retry)
                    if delay is not None:
                        await sleep(delay)
                        continue
                raise

    @staticmethod
    def _valid_result(result):
        if not isinstance(result, ModelResult) or type(result.text) is not str or not result.text.strip():
            raise ModelFailure("invalid_response", True)
        if len(result.text.encode("utf-8")) > 204800:
            raise ModelFailure("output_too_large", True)
        if result.usage is None:
            raise BusinessError("model_usage_unknown", "模型用量未知，请先核对调用。", 409)
        if not isinstance(result.usage, Usage):
            raise ModelFailure("invalid_usage", True)
        result.usage.__post_init__()

    def _history(self, connection, disposition, code=None):
        if self.call_id is None:
            return
        text = None
        request_id = None
        if isinstance(self.result, ModelResult):
            if type(self.result.text) is str:
                try:
                    if len(self.result.text.encode("utf-8")) <= 204800:
                        text = self.result.text
                except UnicodeError:
                    pass
            value = self.result.provider_request_id
            if type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value) and not re.search(
                r"(?:^|[._:-])(?:sk-|CS-|LK-)", value
            ):
                request_id = value
        connection.execute("""UPDATE model_calls SET provider_request_id=COALESCE(provider_request_id,?),
            error_code=COALESCE(?,error_code),response_text=?,response_hash=?,response_disposition=?
            WHERE id=? AND job_id=? AND attempt_id=? AND response_text IS NULL""", (request_id, code, text,
            hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None,
            disposition, self.call_id, self.claim.job_id, self.claim.attempt_id))

    def _known_usage(self):
        if isinstance(self.result, ModelResult) and isinstance(self.result.usage, Usage):
            try:
                self.result.usage.__post_init__()
                return self.result.usage
            except BusinessError:
                pass
        return None

    def _settle_response(self, connection, *, isolated=False):
        if self.call_id is None:
            return
        call = connection.execute("SELECT status FROM model_calls WHERE id=? AND job_id=? AND attempt_id=?",
                                  (self.call_id, self.claim.job_id, self.claim.attempt_id)).fetchone()
        if call is None or call["status"] == "rejected":
            return
        usage = self._known_usage()
        if isolated and call["status"] == "sent":
            self.budget.settle_in_tx(connection, call_id=self.call_id, usage=None, status="unknown")
        if usage is not None:
            self.budget.settle_in_tx(connection, call_id=self.call_id, usage=usage, status="successful")
        elif call["status"] == "sent":
            self.budget.settle_in_tx(connection, call_id=self.call_id, usage=None, status="unknown")

    @staticmethod
    def _complete_step(connection, step_id, stamp, *, outline_id=None, chapter_id=None, candidate_id=None, check_id=None):
        connection.execute("""UPDATE job_steps SET state='succeeded',finished_at=?,outline_revision_id=?,
            chapter_revision_id=?,candidate_id=?,check_id=? WHERE id=? AND state='running'""",
            (stamp, outline_id, chapter_id, candidate_id, check_id, step_id))

    @staticmethod
    def _derived(frozen: ProductionInput, **changes):
        value = replace(frozen, **changes)
        return replace(value, input_hash=hashlib.sha256(
            canonical_json(DraftService._source_payload(value)).encode("utf-8")).hexdigest())

    def _save(self, kind, chapter_id=None):
        data = parse_outline(self.result.text, chapter_count=self.frozen.brief.chapter_count) if kind == "outline" else None
        with transaction(self.repository.db_path, immediate=True) as connection:
            job = self._fence(connection, allow_cancel=True)
            cancelled = bool(job["cancel_requested"])
            stamp = to_db_time(self.repository.clock())
            next_frozen = self.frozen
            if kind == "outline":
                view = self.drafts.apply_outline_in_tx(connection, project_id=self.frozen.project_id,
                    input_hash=self.frozen.input_hash, outline=data, job_id=self.claim.job_id,
                    attempt_id=self.claim.attempt_id, auto_adopt=not cancelled)
                candidate = connection.execute("SELECT id FROM production_candidates WHERE project_id=? AND outline_revision_id=?",
                                               (view.project_id, view.id)).fetchone()
                self._complete_step(connection, self.step_id, stamp, outline_id=view.id,
                                    candidate_id=candidate["id"] if candidate else None)
                if candidate is None:
                    next_frozen = self._derived(self.frozen, outline=view,
                        chapters=tuple(ChapterView(i.chapter_id, None, "") for i in view.items))
            elif kind == "chapter":
                base = next(c for c in self.frozen.chapters if c.id == chapter_id)
                output = self.drafts.record_generated_in_tx(connection, project_id=self.frozen.project_id,
                    chapter_id=chapter_id, body_revision=base.body_revision, input_hash=self.frozen.input_hash,
                    markdown=self.result.text, job_id=self.claim.job_id, attempt_id=self.claim.attempt_id,
                    adopt_empty=not cancelled)
                self._complete_step(connection, self.step_id, stamp, chapter_id=output.revision_id,
                                    candidate_id=output.candidate_id)
                if output.candidate_id is None:
                    source = connection.execute("SELECT revision,markdown FROM chapter_revisions WHERE id=?", (output.revision_id,)).fetchone()
                    next_frozen = self._derived(self.frozen, chapters=tuple(
                        ChapterView(c.id, source["revision"], source["markdown"]) if c.id == chapter_id else c
                        for c in self.frozen.chapters))
            else:
                self._complete_step(connection, self.step_id, stamp)
            self._settle_response(connection)
            self._history(connection, "active")
            self._step_event(connection)
            # Set local derivation only after the entire storage transaction commits.
        self.frozen = next_frozen
        self.call_id, self.result, self.step_id = None, None, None
        if cancelled:
            raise BusinessError("job_cancelled", "任务已请求取消。", 409)

    def _step_event(self, connection):
        append_event(connection, AuditEvent(self.claim.actor.admin_id, "job", str(self.claim.job_id),
            "job.step", "completed", "success", f"job-{self.claim.job_id}", {}))

    def _remaining_plan(self, chapter_ids):
        requests = tuple(build_chapter_request(self.frozen, cid, self.claim.config) for cid in chapter_ids)
        amount = sum(self.budget.estimate(request) for request in requests)
        with transaction(self.repository.db_path, immediate=True) as connection:
            self._fence(connection)
            self._current_matches(connection)
            self.config.require_execution(self.claim.config)
            pool = connection.execute("SELECT * FROM token_reservations WHERE job_id=? AND call_id IS NULL",
                                      (self.claim.job_id,)).fetchone()
            if pool is None or pool["token_limit"] != self.config.task_token_limit:
                raise BusinessError("reservation_conflict", "计划额度条件已变化。", 409)
            available = pool["reserved_tokens"] - pool["released_tokens"]
            if self.budget.effective_tokens_in_tx(connection, job_id=self.claim.job_id) + max(0, amount - available) > pool["token_limit"]:
                raise BusinessError("token_budget_exceeded", "全部剩余章节的计划额度不足。", 409)

    def _validate(self):
        check = validate_draft(self.frozen)
        with transaction(self.repository.db_path, immediate=True) as connection:
            self._fence(connection)
            self._current_matches(connection)
            project = connection.execute("SELECT revision FROM production_projects WHERE id=?", (self.frozen.project_id,)).fetchone()
            check_id = connection.execute("""INSERT INTO draft_checks
                (project_id,project_revision,input_hash,findings_json,blocking,human_review_required,actor_admin_id,created_at)
                VALUES (?,?,?,?,?,1,?,?)""", (self.frozen.project_id, project["revision"], check.input_hash,
                canonical_json([asdict(f) for f in check.findings]), int(check.blocking), self.claim.actor.admin_id,
                to_db_time(self.repository.clock()))).lastrowid
            self._complete_step(connection, self.step_id, to_db_time(self.repository.clock()), check_id=check_id)
            self._step_event(connection)
        self.step_id = None

    @staticmethod
    def _require_complete(connection, claim, expected):
        rows = connection.execute("SELECT * FROM job_steps WHERE job_id=? AND attempt_id=? ORDER BY step_number",
                                  (claim.job_id, claim.attempt_id)).fetchall()
        if len(rows) != len(expected) or any(
            row["step_number"] != number or (row["kind"], row["chapter_id"]) != planned or row["state"] != "succeeded"
            or (row["kind"] == "outline" and row["outline_revision_id"] is None)
            or (row["kind"] == "chapter" and row["chapter_revision_id"] is None)
            or (row["kind"] == "validate" and row["check_id"] is None)
            for number, (row, planned) in enumerate(zip(rows, expected), 1)
        ):
            raise BusinessError("job_steps_incomplete", "任务计划尚未全部完成。", 409)

    def _finish(self):
        with transaction(self.repository.db_path, immediate=True) as connection:
            self._fence(connection)
            self._require_complete(connection, self.claim, self.expected)
            view = self.repository.finish_in_tx(connection, self.claim, "succeeded", None)
            release_unused_pool_in_tx(connection, self.claim.job_id, to_db_time(self.repository.clock()))
            append_event(connection, AuditEvent(self.claim.actor.admin_id, "job", str(self.claim.job_id), "job.finish",
                "completed", "success", f"job-{self.claim.job_id}", {"revision": view.revision, "job_state": view.state}))

    def _isolate(self, code):
        with transaction(self.repository.db_path, immediate=True) as connection:
            self._settle_response(connection, isolated=True)
            self._history(connection, "isolated", code)

    def _fail(self, code):
        try:
            with transaction(self.repository.db_path, immediate=True) as connection:
                job = self._fence(connection, allow_cancel=True)
                self._settle_response(connection)
                self._history(connection, "active", code)
                state = "cancelled" if job["cancel_requested"] else "failed"
                reason = "job_cancelled" if state == "cancelled" else code
                connection.execute("""UPDATE job_steps SET state=?,reason_code=?,finished_at=?
                    WHERE job_id=? AND attempt_id=? AND state IN ('running','pending')""",
                    (state, reason, to_db_time(self.repository.clock()), self.claim.job_id, self.claim.attempt_id))
                view = self.repository.finish_in_tx(connection, self.claim, state, reason)
                release_unused_pool_in_tx(connection, self.claim.job_id, to_db_time(self.repository.clock()))
                append_event(connection, AuditEvent(self.claim.actor.admin_id, "job", str(self.claim.job_id), "job.finish",
                    reason, "success", f"job-{self.claim.job_id}", {"revision": view.revision, "job_state": view.state, "error_code": reason}))
        except BusinessError as error:
            if error.code not in ("attempt_expired", "owner_required"):
                raise
            self._isolate(code)

    async def _run(self):
        if self.kind in ("outline_generate", "course_draft_generate"):
            await asyncio.to_thread(self._new_step, "outline", None)
            await self._call(build_outline_request(self.frozen, self.claim.config))
            await asyncio.to_thread(self._save, "outline")
            if self.kind == "course_draft_generate" and self.claim.input.outline is None and self.frozen.outline is not None:
                self.selected = tuple(c.id for c in self.frozen.chapters)
                self.expected.extend(("chapter", cid) for cid in self.selected)
                self.expected.append(("validate", None))
            else:
                await asyncio.to_thread(self._finish)
                return
        if self.kind in ("course_draft_generate", "chapters_generate", "chapter_regenerate"):
            for index, chapter_id in enumerate(self.selected):
                await asyncio.to_thread(self._remaining_plan, self.selected[index:])
                await asyncio.to_thread(self._new_step, "chapter", chapter_id)
                await self._call(build_chapter_request(self.frozen, chapter_id, self.claim.config))
                await asyncio.to_thread(self._save, "chapter", chapter_id)
        if self.kind in ("course_draft_generate", "chapters_generate", "draft_validate"):
            await asyncio.to_thread(self._new_step, "validate", None)
            await asyncio.to_thread(self._validate)
        elif self.kind == "connection_test":
            await asyncio.to_thread(self._new_step, "connection_test", None)
            await self._call(build_connection_request(self.claim.config))
            await asyncio.to_thread(self._save, "connection_test")
        await asyncio.to_thread(self._finish)
