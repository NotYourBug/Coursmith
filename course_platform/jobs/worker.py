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
        return await self._run_once()

    async def _run_once(self, stop: asyncio.Event | None = None) -> bool:
        async with self._lock:
            await asyncio.to_thread(maintain, self.repository, self.budget)
            if stop is not None and stop.is_set():
                return False
            claim = await asyncio.to_thread(self.repository.claim_next, self.worker_id)
            if claim is None:
                return False
            heartbeat = asyncio.create_task(self._heartbeat(claim))
            try:
                await Handlers.execute(claim, drafts=self.drafts, repository=self.repository,
                    budget=self.budget, model=self.model, config=self.config, stop=stop)
            finally:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
            return True

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if not await self._run_once(stop):
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1)
                except TimeoutError:
                    pass


async def _operate(worker: Worker) -> None:
    """Main-thread signal handlers request cooperation without cancelling a send."""
    import signal

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = {}

    def request_stop(_signal, _frame):
        loop.call_soon_threadsafe(stop.set)

    try:
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, request_stop)
        print('{"status":"worker_ready"}', flush=True)
        await worker.run_forever(stop)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def main(argv: list[str] | None = None) -> int:
    """Fixed maintenance entry. Help/import never load settings or touch SQLite."""
    import json
    import secrets
    import sqlite3
    from contextlib import closing

    from ..cli import _PrivateArgumentParser
    from ..database import LATEST_SCHEMA_VERSION, check_database, open_readonly
    from ..settings import load_settings
    from ..production.config import PROMPT_VERSION, load_production_settings, normalize_endpoint
    from ..production.llm import OpenAIModel

    _PrivateArgumentParser(prog="course-platform worker",
                          description="Run the independent production worker.").parse_args(argv)
    try:
        settings = load_settings()
        if not settings.database_path.is_file() or check_database(settings.database_path)["version"] != LATEST_SCHEMA_VERSION:
            raise BusinessError("offline_migration_required", "请先离线备份并明确迁移数据库。", 409)
        with closing(open_readonly(settings.database_path)) as connection:
            if not connection.execute("SELECT 1 FROM admins WHERE role='owner' AND enabled=1").fetchone():
                raise BusinessError("owner_required", "请先明确初始化有效经营者。", 403)
        config = load_production_settings(settings)
        try:
            config.require_execution()
        except BusinessError as error:
            if error.code not in ("model_context_missing", "model_not_configured"):
                raise
            # Availability must not hide an unsupported execution profile.
            # The original, incomplete config still reaches every per-job gate.
            if config.public.provider != "deepseek" or config.public.model != "deepseek-flash" or (
                config.public.prompt_version != PROMPT_VERSION
            ) or (config.base_url and normalize_endpoint(config.base_url) not in (
                "https://api.deepseek.com/", "https://api.deepseek.com/v1/"
            )):
                raise BusinessError("model_output_unsupported", "模型执行配置不受支持。", 409) from None
            print(json.dumps({"status": "local_only", "error": error.code}), flush=True)
        clock = utc_now
        worker = Worker(JobRepository(settings.database_path, clock=clock),
            DraftService(settings.database_path, clock=clock), TokenBudget(settings.database_path, clock=clock),
            OpenAIModel(config), config, worker_id="worker-" + secrets.token_hex(12), clock=clock)
        asyncio.run(_operate(worker))
        return 0
    except (BusinessError, sqlite3.Error, OSError, ValueError, TypeError) as error:
        # Never print configured paths, source, argv, credentials or SDK messages.
        code = error.code if isinstance(error, BusinessError) else "worker_unavailable"
        print(json.dumps({"error": code}), flush=True)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
