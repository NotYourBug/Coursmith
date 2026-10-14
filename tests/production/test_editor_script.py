"""Real browser toolbar effects: source selection, dirty state and native save."""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


def test_toolbar_wraps_selection_and_saves_exact_markdown(workbench_page):
    page = workbench_page
    editor = page.locator('textarea[name="markdown"]')
    editor.fill("中文😀")
    editor.evaluate("e => e.setSelectionRange(0, 2)")
    page.get_by_role("button", name="粗体", exact=True).click()
    expect(editor).to_have_value("**中文**😀")
    page.get_by_role("button", name="保存正文", exact=True).click()
    expect(editor).to_have_value("**中文**😀")


def test_section_template_fills_all_required_sections(workbench_page):
    page = workbench_page
    page.get_by_role("button", name="课程章节模板", exact=True).click()
    editor = page.locator('textarea[name="markdown"]')
    for section in ("学习目标", "讲解", "案例", "练习", "总结"):
        assert "## " + section in editor.input_value()
    assert page.evaluate("Object.keys(localStorage)") == []
    page.get_by_role("button", name="保存正文", exact=True).click()
    for section in ("学习目标", "讲解", "案例", "练习", "总结"):
        expect(page.locator(".production-preview").get_by_role("heading", name=section, exact=True)).to_be_visible()
