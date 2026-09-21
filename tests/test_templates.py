from pathlib import Path


TEMPLATE_ROOT = Path(__file__).parent.parent / "course_platform"


def test_base_template_has_mobile_viewport():
    base = (TEMPLATE_ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    assert 'name="viewport"' in base
    assert "width=device-width" in base


def test_course_detail_exposes_learning_outcome():
    detail = (TEMPLATE_ROOT / "templates" / "course_detail.html").read_text(encoding="utf-8")
    assert "学完后你将能够" in detail
    assert "免费试学" in detail


def test_chapter_styles_allow_code_overflow_without_page_overflow():
    css = (TEMPLATE_ROOT / "static" / "site.css").read_text(encoding="utf-8")
    assert ".code-block" in css or "pre" in css
    assert "overflow-x: auto" in css
