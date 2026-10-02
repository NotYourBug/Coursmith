import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient as Client

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
    with Client(app) as test_client:
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


def test_free_preview_chapter_is_public(client):
    response = client.get("/courses/fixture-course/chapters/1")

    assert response.status_code == 200
    assert "第一章" in response.text


def test_redeem_redirects_and_sets_http_only_cookie(client, access_code):
    response = client.post(
        "/access/redeem",
        data={"course_slug": "fixture-course", "code": access_code},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "/learn/fixture-course" in response.headers["location"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "course_session_fixture-course" in response.headers["set-cookie"]


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


def test_generated_relative_chapter_links_remain_usable(client, access_code):
    client.post(
        "/access/redeem",
        data={"course_slug": "fixture-course", "code": access_code},
    )

    chapter = client.get("/learn/fixture-course/chapters/01.html")
    index = client.get(
        "/learn/fixture-course/chapters/index.html", follow_redirects=False
    )

    assert chapter.status_code == 200
    assert "第一章" in chapter.text
    assert index.status_code == 303
    assert index.headers["location"] == "/learn/fixture-course"


def test_progress_requires_a_real_json_boolean(client, access_code):
    client.post(
        "/access/redeem",
        data={"course_slug": "fixture-course", "code": access_code},
    )

    response = client.post(
        "/api/progress",
        json={"course_slug": "fixture-course", "chapter_number": 1, "completed": "false"},
    )

    assert response.status_code == 400


def test_security_headers_are_set(client):
    response = client.get("/courses/fixture-course")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert "script-src 'none'" in response.headers["content-security-policy"]
