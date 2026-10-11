"""Local production service fixtures; database and owner stay shared with M1."""

import pytest


@pytest.fixture
def workbench_site(tmp_path, db_path, actor):
    """Test-only real Web process; separate scripted Worker shares its SQLite."""
    import asyncio
    import json
    import os
    import socket
    import subprocess
    import sys
    import threading
    import time
    from pathlib import Path
    from types import SimpleNamespace

    import httpx
    from course_platform.admin.auth import AdminService
    from course_platform.jobs.budget import TokenBudget
    from course_platform.jobs.repository import JobRepository
    from course_platform.jobs.service import JobService
    from course_platform.jobs.worker import Worker
    from course_platform.production.config import load_production_settings
    from course_platform.production.drafts import DraftService
    from course_platform.production.projects import ProjectService
    from course_platform.production.llm import ModelResult, Usage
    from course_platform.settings import Settings
    from tests.jobs.fakes import ScriptedModel

    AdminService(db_path).initialize_owner("owner", "example-pass-123")
    content = tmp_path / "content"
    content.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    settings = Settings("https://api.deepseek.com/", "task10-fake-key", content,
                        db_path, 72, "test", site_origin=url)
    public_env = {"COURSE_MODEL": "deepseek-flash", "COURSE_MODEL_CONTEXT_TOKENS": "1000000",
                  "COURSE_TASK_TOKEN_LIMIT": "200000", "COURSE_MODEL_PRICE_JSON": ""}
    config = load_production_settings(settings, env=public_env)
    drafts, projects = DraftService(db_path), ProjectService(db_path)
    jobs = JobService(db_path, drafts)
    jobs.configure_execution(config, TokenBudget(db_path))
    # Explicit Settings avoids reading a real .env; a socket fence makes any
    # accidental model client/network use fail before contacting a supplier.
    script = '''import os, socket, sys
from pathlib import Path
import uvicorn
from course_platform.settings import Settings
from course_platform.app import create_app
original_connect = socket.socket.connect
original_getaddrinfo = socket.getaddrinfo
def local_getaddrinfo(host, *args, **kwargs):
    if host not in ('127.0.0.1', '::1', 'localhost', None):
        raise RuntimeError('Test Web may only resolve loopback')
    return original_getaddrinfo(host, *args, **kwargs)
def local_connect(sock, address):
    if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1', 'localhost'):
        raise RuntimeError('Test Web may only connect to loopback')
    return original_connect(sock, address)
socket.socket.connect = local_connect
socket.getaddrinfo = local_getaddrinfo
settings = Settings('https://api.deepseek.com/', 'task10-fake-key', Path(sys.argv[1]),
                    Path(sys.argv[2]), 72, 'test', site_origin=sys.argv[3])
uvicorn.run(create_app(settings), host='127.0.0.1', port=int(sys.argv[4]), proxy_headers=False)
'''
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update(public_env)
    env.update(DEEPSEEK_API_KEY="task10-fake-key", PYTHONUTF8="1")
    log_path = tmp_path / "web.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-c", script,
                                    str(content), str(db_path), url, str(port)],
                                   cwd=Path(__file__).resolve().parents[2], env=env,
                                   stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail("Actual Web process failed: " + log_path.read_text(encoding="utf-8"))
                try:
                    if httpx.get(url + "/help", timeout=1, trust_env=False).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(.05)
            else:
                pytest.fail("Actual Web readiness timed out; never skip")

            def run_worker(texts):
                model = ScriptedModel([ModelResult(text, Usage(12, 4), "task10-local") for text in texts])
                worker = Worker(JobRepository(db_path), drafts, TokenBudget(db_path), model,
                                config, worker_id="task10-thread")
                outcomes = []
                def run():
                    try:
                        outcomes.append(asyncio.run(worker.run_once()))
                    except BaseException as error:
                        outcomes.append(error)
                thread = threading.Thread(target=run, daemon=True)
                thread.start()
                thread.join(timeout=20)
                assert not thread.is_alive(), "Separate test worker did not finish"
                assert outcomes == [True], outcomes
                return model

            def outline(count):
                return json.dumps({"items": [{"title": f"第{i}章", "goal": "学习目标"}
                                   for i in range(1, count + 1)]}, ensure_ascii=False)

            yield SimpleNamespace(url=url, db=db_path, actor=actor, drafts=drafts, projects=projects,
                                  jobs=jobs, config=config, run_worker=run_worker, outline=outline,
                                  log=log_path)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


@pytest.fixture
def workbench_context(real_browser, workbench_site):
    context = real_browser.new_context(viewport={"width": 1280, "height": 900})
    def fence(route):
        from urllib.parse import urlsplit
        if urlsplit(route.request.url).hostname in {"127.0.0.1", "localhost", "::1"}:
            route.continue_()
        else:
            route.abort()
    context.route("**/*", fence)
    try:
        yield context
    finally:
        context.close()


def login_workbench(page, site):
    page.goto(site.url + "/admin/login")
    page.locator('input[name="username"]').fill("owner")
    page.locator('input[name="password"]').fill("example-pass-123")
    page.get_by_role("button", name="登录", exact=True).click()
    page.wait_for_url(site.url + "/admin")


def create_workbench_project(page, site, *, title="编辑课程", mode="quick", count=1):
    import json
    page.goto(site.url + "/admin/production/new")
    page.locator('input[name="title"]').fill(title)
    page.locator('select[name="mode"]').select_option(mode)
    page.locator('input[name="chapter_count"]').fill(str(count))
    page.get_by_role("button", name="创建课程项目", exact=True).click()
    project_id = int(page.url.rsplit("/", 1)[1])
    page.locator('textarea[name="outline_json"]').fill(json.dumps([
        {"chapter_id": None, "title": f"章节{i}", "goal": "学习"} for i in range(1, count + 1)]))
    page.get_by_role("button", name="保存大纲", exact=True).click()
    return project_id


@pytest.fixture
def workbench_page(workbench_context, workbench_site):
    page = workbench_context.new_page()
    login_workbench(page, workbench_site)
    project_id = create_workbench_project(page, workbench_site)
    chapter = workbench_site.drafts.freeze_input(project_id).chapters[0]
    page.goto(f"{workbench_site.url}/admin/production/{project_id}/chapters/{chapter.id}")
    yield page


@pytest.fixture
def project_service(db_path, clock):
    from course_platform.production.projects import ProjectService

    return ProjectService(db_path, clock=clock.now)

@pytest.fixture
def production_client(admin_settings, clock, monkeypatch):
    """Real factory/lifespan/login; never inject the new factory-owned services."""
    import re
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from course_platform.app import create_app

    monkeypatch.setattr("course_platform.app.utc_now", clock.now)
    for key in ("COURSE_MODEL", "COURSE_MODEL_CONTEXT_TOKENS", "COURSE_TASK_TOKEN_LIMIT", "COURSE_MODEL_PRICE_JSON"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COURSE_MODEL_CONTEXT_TOKENS", "1000000")
    settings = replace(admin_settings, base_url="https://api.deepseek.com/", api_key="task9-fake-api-key")
    app = create_app(settings)
    with TestClient(app, client=("198.51.100.7", 50000), follow_redirects=False) as client:
        app.state.admin_service.initialize_owner("owner", "example-pass-123")
        page = client.get("/admin/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        assert client.post("/admin/login", data={"username": "owner", "password": "example-pass-123",
            "csrf_token": token}, headers={"Origin": "http://testserver"}).status_code == 303
        yield client


@pytest.fixture
def production_data(production_client, db_path, clock, actor):
    from types import SimpleNamespace
    from course_platform.production.projects import ProjectService
    from course_platform.production.drafts import DraftService
    from course_platform.production.models import BriefInput, OutlineInput, OutlineItem

    projects, drafts = ProjectService(db_path, clock=clock.now), DraftService(db_path, clock=clock.now)
    project = projects.create(actor, BriefInput(title="私有课程", chapter_count=2), mode="quick")
    saved = drafts.save_outline(actor, project.id, project.revision, OutlineInput(items=[
        OutlineItem(chapter_id=None, title="章节一", goal="学习"),
        OutlineItem(chapter_id=None, title="章节二", goal="实践")]))
    return SimpleNamespace(client=production_client, projects=projects, drafts=drafts, project_id=project.id,
        chapter_id=saved.items[0].chapter_id, actor=actor, db_path=db_path, clock=clock)
