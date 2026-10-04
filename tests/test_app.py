"""Original public consumers now run real production services and POST security."""
import pytest

from test_delivery_routes import redeem


@pytest.fixture
def client(delivery_client):
    return delivery_client


@pytest.fixture
def access_code(client, actor):
    return client.app.state.access_service.create_access_code("fixture-course", actor=actor)


def test_course_detail_is_public(client):
    response = client.get("/courses/fixture-course")
    assert response.status_code == 200
    assert "可售课程" in response.text


def test_chapter_requires_access(client):
    assert client.get("/learn/fixture-course/chapters/1").status_code == 403


def test_free_preview_chapter_is_public(client):
    response = client.get("/courses/fixture-course/chapters/1")
    assert response.status_code == 200 and "第一章" in response.text


def test_redeem_result_sets_http_only_cookie(client, access_code):
    response = redeem(client, access_code)
    assert response.status_code == 200
    assert "/learn/fixture-course" in response.text
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    assert "Path=/" in response.headers["set-cookie"]
    assert "course_session_fixture-course" in response.headers["set-cookie"]


def test_authorized_user_can_open_chapter_and_save_progress(client, access_code):
    redeem(client, access_code)
    page = client.get("/learn/fixture-course/chapters/1")
    saved = client.post("/api/progress", json=dict(course_slug="fixture-course", chapter_number=1,
        completed=True, csrf_token=client.cookies.get("course_csrf_fixture-course")),
        headers={"Origin": "http://testserver", "X-CSRF-Token": client.cookies.get("course_csrf_fixture-course")})
    assert page.status_code == 200 and saved.status_code == 204


def test_generated_relative_chapter_links_remain_usable(client, access_code):
    redeem(client, access_code)
    chapter = client.get("/learn/fixture-course/chapters/01.html")
    index = client.get("/learn/fixture-course/chapters/index.html")
    assert chapter.status_code == 200 and "第一章" in chapter.text
    assert index.status_code == 303 and index.headers["location"] == "/learn/fixture-course"


def test_progress_requires_a_real_json_boolean(client, access_code):
    redeem(client, access_code)
    response = client.post("/api/progress", json=dict(course_slug="fixture-course", chapter_number=1,
        completed="false", csrf_token=client.cookies.get("course_csrf_fixture-course")),
        headers={"Origin": "http://testserver", "X-CSRF-Token": client.cookies.get("course_csrf_fixture-course")})
    assert response.status_code == 400


def test_security_headers_are_set(client):
    response = client.get("/courses/fixture-course")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "script-src 'none'" in response.headers["content-security-policy"]
