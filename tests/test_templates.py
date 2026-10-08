from pathlib import Path


TEMPLATE_ROOT = Path(__file__).parent.parent / "course_platform"


def test_base_template_has_mobile_viewport(delivery_client):
    response = delivery_client.get("/")
    assert response.status_code == 200
    base = response.text
    assert 'name="viewport"' in base
    assert "width=device-width" in base


def test_course_detail_exposes_learning_outcome(delivery_client):
    response = delivery_client.get("/courses/fixture-course")
    assert response.status_code == 200
    detail = response.text
    assert "学完后你将能够" in detail
    assert "免费试学" in detail
    assert "完成练习" in detail and "邮件支持" in detail


def test_chapter_styles_allow_code_overflow_without_page_overflow(delivery_client):
    response = delivery_client.get("/static/site.css")
    assert response.status_code == 200
    css = response.text
    assert ".code-block" in css or "pre" in css
    assert "overflow-x: auto" in css
