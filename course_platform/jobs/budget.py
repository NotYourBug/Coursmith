"""Transactional accounting: transfer planned capacity to distinct call ledgers."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from ..database import to_db_time
from ..domain import BusinessError, Clock, utc_now
from ..production.config import ProductionSettings, strict_integer
from ..production.llm import ModelRequest, Usage, estimated_tokens, serialize_model_payload
from .models import Claim, canonical_json
from .repository import JobRepository


class TokenBudget:
    def __init__(self, db_path: Path, *, clock: Clock = utc_now):
        self.db_path = Path(db_path)
        self.clock = clock
        self._config: ProductionSettings | None = None

    def configure_execution(self, config: ProductionSettings) -> None:
        if not isinstance(config,ProductionSettings):
            raise BusinessError("invalid_model_config","模型执行配置无效。",409)
        config.__post_init__()
        self._config = config

    def estimate(self, request: ModelRequest) -> int:
        return estimated_tokens(request)

    @staticmethod
    def _tx(connection: sqlite3.Connection) -> None:
        if not connection.in_transaction:
            raise BusinessError("job_transaction","额度写入需要活动事务。",409)

    def reserve_in_tx(self, connection: sqlite3.Connection, *, job_id: int, planned_tokens: int, limit: int) -> None:
        self._tx(connection)
        strict_integer(job_id,minimum=1)
        strict_integer(planned_tokens)
        strict_integer(limit,minimum=1)
        if planned_tokens>limit:
            raise BusinessError("token_budget_exceeded","计划生成额度超限，请减少章节或资料，或联系维护者。",409)
        prior = connection.execute("SELECT * FROM token_reservations WHERE job_id=? AND call_id IS NULL",(job_id,)).fetchone()
        if prior is not None:
            if prior["reserved_tokens"]!=planned_tokens or prior["token_limit"]!=limit:
                raise BusinessError("reservation_conflict","任务预留已冻结，请创建新任务。",409)
            return
        connection.execute("INSERT INTO token_reservations (job_id,reserved_tokens,token_limit,created_at) VALUES (?,?,?,?)",
            (job_id,planned_tokens,limit,to_db_time(self.clock())))

    def effective_tokens_in_tx(self, connection: sqlite3.Connection, *, job_id: int) -> int:
        """Outstanding pool + each call's actual known usage or unreleased reserve."""
        self._tx(connection)
        total = 0
        for row in connection.execute("SELECT * FROM token_reservations WHERE job_id=?",(job_id,)):
            if row["call_id"] is None:
                total += row["reserved_tokens"]-row["released_tokens"]
            elif row["used_tokens"] is not None:
                total += row["used_tokens"]
            else:
                total += row["reserved_tokens"]-row["released_tokens"]
        return total

    def begin_call_in_tx(self, connection: sqlite3.Connection, claim: Claim, request: ModelRequest) -> int:
        self._tx(connection)
        JobRepository(self.db_path,clock=self.clock).require_attempt_in_tx(connection,claim,self.clock())
        job = connection.execute("SELECT * FROM production_jobs WHERE id=?",(claim.job_id,)).fetchone()
        if job["cancel_requested"]:
            raise BusinessError("job_cancelled","任务取消后不能启动新的模型请求。",409)
        if self._config is None:
            raise BusinessError("model_not_configured","未配置模型执行，请联系维护者。",409)
        self._config.require_execution(claim.config)
        if job["config_json"] != canonical_json(claim.config.model_dump(warnings=False)):
            raise BusinessError("model_config_changed", "执行条件不符合原任务冻结配置。", 409)
        if request.config != claim.config:
            raise BusinessError("model_config_changed","模型执行条件已变化，请创建新任务。",409)
        output_limit = 32 if job["kind"] == "connection_test" else (
            claim.config.outline_output_tokens if job["kind"] == "outline_generate" or (
                job["kind"] == "course_draft_generate" and request.messages[0]["content"].startswith("Generate a course outline")
            ) else claim.config.chapter_output_tokens)
        if request.max_output_tokens > output_limit:
            raise BusinessError("invalid_model_request", "请求输出额度超过冻结上限。", 409)
        from ..production.prompts import require_request_limits

        require_request_limits(request)
        amount = self.estimate(request)
        if connection.execute("SELECT 1 FROM model_calls WHERE job_id=? AND status='unknown'",(claim.job_id,)).fetchone():
            raise BusinessError("model_usage_unknown","先核对未知调用，再明确创建恢复任务。",409)
        if connection.execute("SELECT 1 FROM model_calls WHERE status='sent'").fetchone():
            raise BusinessError("model_call_pending","模型调用尚未完成，不能开始新调用。",409)
        pool = connection.execute("SELECT * FROM token_reservations WHERE job_id=? AND call_id IS NULL",(claim.job_id,)).fetchone()
        if pool is None or pool["state"]!="reserved" or pool["token_limit"]!=self._config.task_token_limit:
            raise BusinessError("reservation_conflict","预留执行条件已变化，请创建新任务。",409)
        available = pool["reserved_tokens"]-pool["released_tokens"]
        transfer = min(available,amount)
        if self.effective_tokens_in_tx(connection,job_id=claim.job_id)+amount-transfer>pool["token_limit"]:
            raise BusinessError("token_budget_exceeded","剩余额度不足，不能开始模型调用。",409)
        steps = connection.execute("SELECT id FROM job_steps WHERE job_id=? AND attempt_id=? AND state='running'",(claim.job_id,claim.attempt_id)).fetchall()
        if len(steps)>1:
            raise BusinessError("job_step_conflict","任务步骤冲突。",409)
        number = connection.execute("SELECT COALESCE(MAX(call_number),0)+1 FROM model_calls WHERE attempt_id=?",(claim.attempt_id,)).fetchone()[0]
        strict_integer(number,minimum=1)
        now = to_db_time(self.clock())
        price = canonical_json(self._config.price.to_dict()) if self._config.price else None
        input_hash = claim.input.input_hash if claim.input else job["input_hash"]
        call_id = connection.execute("""INSERT INTO model_calls
            (job_id,attempt_id,step_id,call_number,provider,model,endpoint_fingerprint,prompt_version,input_hash,request_hash,
             reserved_tokens,price_json,started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (claim.job_id,claim.attempt_id,steps[0]["id"] if steps else None,number,claim.config.provider,claim.config.model,
                claim.config.endpoint_fingerprint,claim.config.prompt_version,input_hash,hashlib.sha256(serialize_model_payload(request)).hexdigest(),amount,price,now)).lastrowid
        # Aggregate released_tokens records transferred capacity, not zero usage.
        # Identity amounts stay immutable; per-call used/released fields settle it.
        connection.execute("UPDATE token_reservations SET released_tokens=released_tokens+? WHERE id=?",(transfer,pool["id"]))
        connection.execute("INSERT INTO token_reservations (job_id,call_id,reserved_tokens,token_limit,created_at) VALUES (?,?,?,?,?)",
            (claim.job_id,call_id,amount,pool["token_limit"],now))
        return call_id

    def settle_in_tx(self, connection: sqlite3.Connection, *, call_id: int, usage: Usage | None, status: str) -> None:
        """Trusted producer only: rejected requires adapter proof and Usage(0,0)."""
        self._tx(connection)
        strict_integer(call_id,minimum=1)
        if status not in ("successful","rejected","unknown") or (usage is not None and not isinstance(usage,Usage)):
            raise BusinessError("invalid_model_settlement","模型结算状态无效。",409)
        if usage is not None:
            usage.__post_init__()
        if (status=="unknown" and usage is not None) or (status!="unknown" and usage is None) or (status=="rejected" and usage.input_tokens+usage.output_tokens!=0):
            raise BusinessError("invalid_model_settlement","未知用量不能作为零用量结算。",409)
        call = connection.execute("SELECT * FROM model_calls WHERE id=?",(call_id,)).fetchone()
        reservation = connection.execute("SELECT * FROM token_reservations WHERE call_id=?",(call_id,)).fetchone()
        if call is None or reservation is None:
            raise BusinessError("model_call_missing","模型调用账目不存在。",404)
        wanted = (usage.input_tokens,usage.output_tokens) if usage else (None,None)
        if call["status"]!="sent":
            if (call["status"]==status or (call["status"]=="unknown" and status=="successful")) and (call["input_tokens"],call["output_tokens"])==wanted:
                return
            # Later known usage is allowed only through trusted late-response or
            # explicit reconciliation code. Keep the original unknown status.
            if call["status"]!="unknown" or call["input_tokens"] is not None or status!="successful":
                raise BusinessError("settlement_conflict","调用已结算，不能重复释放或覆盖账目。",409)
        final_status = "unknown" if call["status"]=="unknown" else status
        total = usage.input_tokens+usage.output_tokens if usage else None
        released = max(0,reservation["reserved_tokens"]-total) if total is not None else 0
        state = "released" if status=="rejected" else "settled" if usage else "unknown"
        now = to_db_time(self.clock())
        connection.execute("UPDATE model_calls SET status=?,input_tokens=?,output_tokens=?,finished_at=? WHERE id=?",
            (final_status,*wanted,now,call_id))
        connection.execute("UPDATE token_reservations SET state=?,used_tokens=?,released_tokens=?,settled_at=? WHERE call_id=?",
            (state,total,released,now,call_id))
