"""Explicit independent worker and reusable non-network interruption maintenance."""

from __future__ import annotations

import asyncio
from asyncio import sleep
from contextlib import suppress

from ..database import to_db_time, transaction
from ..domain import BusinessError, Clock, utc_now
from ..production.config import ProductionSettings
from ..production.drafts import DraftService
from ..production.llm import ModelClient
from .budget import TokenBudget
from .handlers import Handlers, release_unused_pool_in_tx
from .repository import JobRepository


def maintain(repository: JobRepository, budget: TokenBudget, *, worker_id: str | None = None) -> int:
    """Shared trusted coordinator for periodic worker and explicit admin checks.

    Repository interruption commits first. The repeatable ledger pass repairs even
    an interruption committed before a process died, without replaying any call.
    """
    if repository.db_path != budget.db_path:
        raise BusinessError("job_database", "任务与额度数据库必须一致。", 409)
    count = repository.mark_interrupted(worker_id=worker_id)
    with transaction(repository.db_path, immediate=True) as connection:
        calls = connection.execute("""SELECT c.id,c.job_id FROM model_calls c
            JOIN job_attempts a ON a.id=c.attempt_id AND a.job_id=c.job_id
            WHERE c.status='sent' AND a.state!='running' AND (? IS NULL OR a.worker_id=?)""",
            (worker_id, worker_id)).fetchall()
        for call in calls:
            budget.settle_in_tx(connection, call_id=call["id"], usage=None, status="unknown")
            connection.execute("UPDATE model_calls SET error_code='worker_lost' WHERE id=?", (call["id"],))
        terminal = connection.execute("""SELECT j.id,j.state FROM production_jobs j
            JOIN job_attempts a ON a.job_id=j.id WHERE j.state IN ('succeeded','failed','cancelled','interrupted')
            AND (? IS NULL OR a.worker_id=?)""", (worker_id, worker_id)).fetchall()
        stamp = to_db_time(repository.clock())
        for job in terminal:
            release_unused_pool_in_tx(connection, job["id"], stamp)
            connection.execute("""UPDATE job_steps SET state=?,reason_code='worker_lost',finished_at=?
                WHERE job_id=? AND state IN ('pending','running')""", (job["state"], stamp, job["id"]))
    return count


class Worker:
    def __init__(self, repository: JobRepository, drafts: DraftService, budget: TokenBudget,
                 model: ModelClient, config: ProductionSettings, *, worker_id: str, clock: Clock = utc_now):
        if repository.db_path != drafts.db_path or repository.db_path != budget.db_path:
            raise BusinessError("job_database", "任务、草稿与额度数据库必须一致。", 409)
        self.repository, self.drafts, self.budget = repository, drafts, budget
        self.model, self.config, self.worker_id, self.clock = model, config, worker_id, clock
        budget.configure_execution(config)
        self._lock = asyncio.Lock()

    async def _heartbeat(self, claim):
        while True:
            await sleep(10)
            try:
                await asyncio.to_thread(self.repository.heartbeat, claim)
            except BusinessError:
                return

    async def run_once(self) -> bool:
        async with self._lock:
            await asyncio.to_thread(maintain, self.repository, self.budget)
            claim = await asyncio.to_thread(self.repository.claim_next, self.worker_id)
            if claim is None:
                return False
            heartbeat = asyncio.create_task(self._heartbeat(claim))
            try:
                await Handlers.execute(claim, drafts=self.drafts, repository=self.repository,
                    budget=self.budget, model=self.model, config=self.config)
            finally:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
            return True

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if not await self.run_once():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1)
                except TimeoutError:
                    pass
