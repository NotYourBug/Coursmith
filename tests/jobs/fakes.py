"""Controlled model/client fixtures: no environment credentials or external traffic."""

import asyncio
import json
from dataclasses import replace

import httpx

from course_platform.settings import Settings


def production_settings(tmp_path, *, env=None, **changes):
    from course_platform.production.config import load_production_settings

    source = Settings(base_url="https://api.deepseek.com/", api_key="fake-private-key",
        content_root=tmp_path / "content", database_path=tmp_path / "test.db",
        session_ttl_hours=72, environment="development")
    config = load_production_settings(source, env={"COURSE_MODEL":"deepseek-flash", "COURSE_MODEL_CONTEXT_TOKENS":"1000000", **(env or {})})
    return replace(config, **changes)


def response_body(text="# 内容😀", usage=None, **changes):
    value = {"id":"cmpl-test", "object":"chat.completion", "created":1, "model":"deepseek-flash",
        "choices":[{"index":0, "finish_reason":"stop", "logprobs":None,
            "message":{"role":"assistant", "content":text}}],
        "usage": usage if usage is not None else {"prompt_tokens":12, "completion_tokens":4, "total_tokens":16}}
    value.update(changes)
    return json.dumps(value, ensure_ascii=False, separators=(",",":"), allow_nan=False).encode()


class MemoryStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk

    async def aclose(self):
        self.closed = True


class ModelTransport(httpx.AsyncBaseTransport):
    """Records real adapter HTTP bytes. Unscripted calls cannot reach a socket."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def handle_async_request(self, request):
        content = await request.aread()
        self.calls.append((str(request.url), content, dict(request.headers)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, bytes):
            return httpx.Response(200, headers={"content-type":"application/json"}, stream=MemoryStream([response]), request=request)
        return response


class ScriptedModel:
    def __init__(self, results, *, barrier=None):
        self.results = list(results)
        self.calls = []
        self.sent = asyncio.Event()
        self.barrier = barrier

    async def generate(self, request, *, deadline_seconds=120):
        self.calls.append(request)
        self.sent.set()
        if self.barrier is not None:
            await self.barrier.wait()
        value = self.results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


# Process fixtures below replace only time/configuration and external transport.
# Actual CLI, Web lifespan, SDK, Worker, handlers and database services run intact.
PROCESS_BODY = "## 学习目标\n了解。\n## 讲解\n解释。\n## 案例\n例子。\n## 练习\n实践。\n## 总结\n回顾。"


def isolated_process_env(root, db_path, content, *, configured=True):
    import os

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update(COURSE_DATABASE=str(db_path), COURSE_CONTENT_ROOT=str(content),
        COURSE_ENVIRONMENT="test", COURSE_SITE_ORIGIN="http://127.0.0.1:8000",
        COURSE_SESSION_TTL_HOURS="72", COURSE_TRUSTED_PROXY_CIDRS="",
        COURSE_BASE_URL="https://api.deepseek.com/", DEEPSEEK_API_KEY="task12-fake-key" if configured else "",
        COURSE_MODEL="deepseek-flash", COURSE_MODEL_CONTEXT_TOKENS="1000000" if configured else "",
        COURSE_TASK_TOKEN_LIMIT="200000", COURSE_MODEL_PRICE_JSON="", PYTHONUTF8="1")
    return env


def wait_until(predicate, *, seconds=15):
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.04)
    return False


class LocalSupplier:
    """Real HTTP supplier with independently observed acceptance/response barrier."""
    def __init__(self, texts, *, hold=1):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.texts, self.hold, self.requests = list(texts), hold, []
        self.release = threading.Event()
        supplier = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                payload = json.loads(body)
                supplier.requests.append(payload)
                number = len(supplier.requests)
                if number == supplier.hold:
                    supplier.release.wait(30)
                text = supplier.texts[number - 1] if number <= len(supplier.texts) else PROCESS_BODY
                data = response_body(text, id=f"task12-local-{number}")
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        assert not self.thread.is_alive()


class ProcessHarness:
    """Owns only known test child PIDs, files and a shared isolated SQLite DB."""
    def __init__(self, root, db_path, actor, clock):
        import socket
        from pathlib import Path
        from course_platform.jobs.budget import TokenBudget
        from course_platform.jobs.service import JobService
        from course_platform.production.config import load_production_settings
        from course_platform.production.drafts import DraftService
        from course_platform.production.projects import ProjectService

        self.root, self.db, self.actor, self.clock = root, db_path, actor, clock
        self.source = Path(__file__).resolve().parents[2]
        self.content = root / "process-content"
        self.content.mkdir()
        self.clock_path = root / "process-clock.txt"
        self.write_clock()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.env = isolated_process_env(root, db_path, self.content)
        self.env["COURSE_SITE_ORIGIN"] = self.url
        settings = Settings("https://api.deepseek.com/", "task12-fake-key", self.content,
                            db_path, 72, "test", site_origin=self.url)
        self.config = load_production_settings(settings, env=self.env)
        self.drafts = DraftService(db_path, clock=clock.now)
        self.projects = ProjectService(db_path, clock=clock.now)
        self.jobs = JobService(db_path, self.drafts, clock=clock.now)
        self.budget = TokenBudget(db_path, clock=clock.now)
        self.jobs.configure_execution(self.config, self.budget)
        self.children, self.logs, self.suppliers = [], [], []

    def write_clock(self):
        import os
        import time

        pending = self.clock_path.with_suffix(".next")
        pending.write_text(self.clock.now().isoformat(), encoding="utf-8")
        for attempt in range(151):
            try:
                os.replace(pending, self.clock_path)
                return
            except PermissionError:
                if os.name != "nt" or attempt == 150:
                    raise
                # A Windows reader can briefly prevent replacing the old file.
                time.sleep(.02)

    def rows(self, query, parameters=()):
        from contextlib import closing
        from course_platform.database import open_readonly

        with closing(open_readonly(self.db)) as conn:
            return [dict(row) for row in conn.execute(query, parameters)]

    def supplier(self, texts, *, hold=1):
        supplier = LocalSupplier(texts, hold=hold)
        self.suppliers.append(supplier)
        return supplier

    def spawn(self, mode, supplier=None, *, direct=False):
        import os
        import subprocess
        import sys

        ordinal = len(self.children)
        log_path = self.root / f"task12-{mode}-{ordinal}.log"
        control = self.root / f"task12-signal-{ordinal}.txt"
        proof = self.root / f"task12-signal-proof-{ordinal}.txt"
        log = log_path.open("wb")
        args = [sys.executable, "-X", "utf8", "-m", "tests.jobs.fakes", mode,
                str(self.clock_path), str(self.port), supplier.url if supplier else "",
                str(control), str(proof), "direct" if direct else "cli"]
        process = subprocess.Popen(args, cwd=self.source, env=self.env, stdout=log, stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.children.append(process)
        self.logs.append(log)
        process.task12_log, process.task12_control, process.task12_proof = log_path, control, proof
        return process

    def web(self):
        process = self.spawn("web")
        def ready():
            if process.poll() is not None:
                return False
            try:
                return httpx.get(self.url + "/help", timeout=.5, trust_env=False).status_code == 200
            except httpx.TransportError:
                return False
        assert wait_until(ready), process.task12_log.read_text(encoding="utf-8")
        return process

    def signal(self, process, name="SIGINT"):
        # Windows hidden children have no console for CTRL_BREAK delivery. The
        # test-only bridge raises a genuine CRT signal in the child. It neither
        # calls Worker.stop nor replaces the production signal handler.
        pending = process.task12_control.with_suffix(".pending")
        pending.write_text(name, encoding="ascii")
        pending.replace(process.task12_control)
        assert wait_until(lambda: process.task12_proof.exists() and
                          process.task12_proof.read_text(encoding="ascii")), "Child did not raise the requested OS signal"
        return process.task12_proof.read_text(encoding="ascii")

    def stop(self, process, *, kill=False):
        import subprocess

        if process.poll() is None:
            process.kill() if kill else process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def close(self):
        for process in reversed(self.children):
            self.stop(process)
        for supplier in self.suppliers:
            supplier.close()
        for log in self.logs:
            log.close()
        assert all(process.poll() is not None for process in self.children)


def _process_entry():
    """Test-only transport/time bootstrap; no production flag or whitelist change."""
    import signal
    import socket
    import sys
    import threading
    import time
    from datetime import datetime
    from pathlib import Path
    import course_platform.settings as settings_module
    import course_platform.production.llm as llm
    import course_platform.jobs.worker as worker_module

    mode, clock_file, port, supplier_url, control_file, proof_file, entry = sys.argv[1:]
    settings_module._read_dotenv = lambda _: {}
    original_connect, original_dns = socket.socket.connect, socket.getaddrinfo

    def local_dns(host, *args, **kwargs):
        if host not in ("127.0.0.1", "localhost", "::1", None):
            raise RuntimeError("Test process only resolves loopback")
        return original_dns(host, *args, **kwargs)

    def local_connect(sock, address):
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "localhost", "::1"):
            raise RuntimeError("Test process only connects to loopback")
        return original_connect(sock, address)

    socket.getaddrinfo, socket.socket.connect = local_dns, local_connect

    def clock():
        return datetime.fromisoformat(Path(clock_file).read_text(encoding="utf-8"))

    worker_module.utc_now = clock
    if mode == "web":
        import logging
        import uvicorn
        import course_platform.app as app_module

        app_module.utc_now = clock
        logging.basicConfig(level=logging.INFO)
        uvicorn.run(app_module.create_app(settings_module.load_settings()),
                    host="127.0.0.1", port=int(port), proxy_headers=False, access_log=False)
        return 0

    original_model = llm.OpenAIModel

    class ForwardTransport(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            if request.url.host != "api.deepseek.com":
                raise RuntimeError("Unexpected supplier route")
            body = await request.aread()
            local = httpx.Request(request.method, supplier_url + request.url.path,
                                  headers=request.headers, content=body)
            return await super().handle_async_request(local)

    class LocalHTTPModel:
        def __init__(self, config):
            self.config = config

        async def generate(self, request, *, deadline_seconds=120):
            client = httpx.AsyncClient(transport=ForwardTransport(retries=0), trust_env=False,
                                      follow_redirects=False, timeout=deadline_seconds)
            return await original_model(self.config, http_client=client).generate(
                request, deadline_seconds=deadline_seconds)

    llm.OpenAIModel = LocalHTTPModel

    if mode == "late":
        from contextlib import closing
        from course_platform.database import from_db_time, open_readonly, transaction
        from course_platform.domain import Actor, BusinessError
        from course_platform.jobs.budget import TokenBudget
        from course_platform.jobs.handlers import Handlers
        from course_platform.jobs.models import Claim
        from course_platform.jobs.repository import JobRepository
        from course_platform.production.config import load_production_settings
        from course_platform.production.drafts import DraftService

        settings = settings_module.load_settings()
        config = load_production_settings(settings)
        repository = JobRepository(settings.database_path, clock=clock)
        drafts = DraftService(settings.database_path, clock=clock)
        budget = TokenBudget(settings.database_path, clock=clock)
        budget.configure_execution(config)
        with closing(open_readonly(settings.database_path)) as conn:
            job = conn.execute("SELECT * FROM production_jobs WHERE state='interrupted' ORDER BY id LIMIT 1").fetchone()
            attempt = conn.execute("SELECT * FROM job_attempts WHERE job_id=?", (job["id"],)).fetchone()
            call = conn.execute("SELECT * FROM model_calls WHERE attempt_id=?", (attempt["id"],)).fetchone()
            step = conn.execute("SELECT * FROM job_steps WHERE attempt_id=?", (attempt["id"],)).fetchone()
            claim = Claim(job["id"], attempt["id"], attempt["attempt_token"], from_db_time(attempt["lease_until"]),
                Actor(job["actor_admin_id"], "task12-late"), JobRepository._input(conn, job), config.public)
        before = drafts.freeze_input(job["project_id"])
        execution = Handlers(claim, drafts=drafts, repository=repository, budget=budget,
                             model=LocalHTTPModel(config), config=config)
        execution.kind, execution.call_id, execution.step_id = job["kind"], call["id"], step["id"]
        execution.result = llm.ModelResult(PROCESS_BODY, llm.Usage(12, 4), "task12-late-result")
        code = None
        try:
            execution._save("chapter", step["chapter_id"])
        except BusinessError as error:
            code = error.code
            execution._isolate("worker_lost")
        with transaction(settings.database_path, immediate=True) as conn:
            try:
                repository.require_attempt_in_tx(conn, claim, clock())
            except BusinessError as error:
                assert error.code == "attempt_expired"
        print(json.dumps({"fence": code, "human_unchanged": before == drafts.freeze_input(job["project_id"])}), flush=True)
        return 0

    def signal_bridge():
        while not Path(control_file).exists():
            time.sleep(.02)
        name = Path(control_file).read_text(encoding="ascii")
        Path(proof_file).write_text(name + ":raised_in_child", encoding="ascii")
        signal.raise_signal(getattr(signal, name))

    threading.Thread(target=signal_bridge, daemon=True).start()
    if entry == "direct":
        from course_platform.jobs.budget import TokenBudget
        from course_platform.jobs.repository import JobRepository
        from course_platform.production.drafts import DraftService
        from course_platform.production.config import load_production_settings

        async def run():
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
            settings = settings_module.load_settings()
            config = load_production_settings(settings)
            worker = worker_module.Worker(JobRepository(settings.database_path, clock=clock),
                DraftService(settings.database_path, clock=clock), TokenBudget(settings.database_path, clock=clock),
                LocalHTTPModel(config), config, worker_id="task12-direct", clock=clock)
            print("worker_ready", flush=True)
            await worker.run_forever(stop)
        asyncio.run(run())
        return 0
    from course_platform.cli import main
    return main(["worker"])


if __name__ == "__main__":
    raise SystemExit(_process_entry())
