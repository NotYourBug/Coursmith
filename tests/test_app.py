import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from course_platform.access import AccessService
from course_platform.app import create_app
from course_platform.settings import Settings


@pytest.fixture
def client(tmp_path):
    source = Path(__file__).parent / "fixtures" / "course-package"
    content_root = tmp_path / "content" / "courses"
    shutil.copytree(source, content_root / "fixture-course")
    settings = Settings(
        base_url="https://api.deepseek.com/",
        api_key="",
        content_root=content_root,
        database_path=tmp_path / "data" / "course.db",
        session_ttl_hours=72,
        environment="test",
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def access_code(client):
    service = client.app.state.access_service
    return service.create_access_code("fixture-course")


def test_course_detail_is_public(client):
    response = client.get("/courses/fixture-course")

    assert response.status_code == 200
    assert "Fixture Course" in response.text


def test_chapter_requires_access(client):
    response = client.get("/learn/fixture-course/chapters/1")

    assert response.status_code == 403


def test_redeem_redirects_and_sets_http_only_cookie(client, access_code):
    response = client.post(
        "/access/redeem",
        data={"course_slug": "fixture-course", "code": access_code},
    )

    assert response.status_code == 303
    assert "/learn/fixture-course" in response.headers["location"]
    assert "HttpOnly" in response.headers["set-cookie"]


def test_authorized_user_can_open_chapter_and_save_progress(client, access_code):
    client.post(
        "/access/redeem",
        data={"course_slug": "fixture-course", "code": access_code},
    )

    page = client.get("/learn/fixture-course/chapters/1")
    saved = client.post(
        "/api/progress",
        json={"course_slug": "fixture-course", "chapter_number": 1, "completed": True},
    )

    assert page.status_code == 200
    assert saved.status_code == 204
