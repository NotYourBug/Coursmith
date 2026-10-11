"""Local production service fixtures; database and owner stay shared with M1."""

import pytest


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
