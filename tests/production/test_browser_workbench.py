"""Actual Chrome native flows; broken save/confirmation/safety/layout fail here."""
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from tests.production.conftest import create_workbench_project, login_workbench

pytestmark = pytest.mark.browser
BODY = "## 学习目标\n掌握方法\n## 讲解\n说明\n## 案例\n演示\n## 练习\n实践\n## 总结\n复盘"
PROBE = ('\n</textarea><script>window.__candidateExecuted += 1</script>\n'
         '<img src="https://external.invalid/raw" onerror="window.__candidateExecuted += 1">\n'
         '![外部图片](https://external.invalid/image)\n'
         '[外部链接](https://external.invalid/link)\n'
         '[危险](javascript:window.__candidateExecuted++)')


def generate(page, kind):
    form = page.locator('form[action$="/generate"]')
    form.locator('select[name="kind"]').select_option(kind)
    form.get_by_role("button").click()
    expect(page.get_by_role("heading", name="执行轮次与步骤")).to_be_visible()
    return int(page.url.rsplit("/", 1)[1])


def test_title_only_to_draft_and_precise_confirmation(workbench_context, workbench_site):
    page, site = workbench_context.new_page(), workbench_site
    login_workbench(page, site)
    page.goto(site.url + "/admin/production/new")
    expect(page.get_by_text("默认配置", exact=True)).to_be_visible()
    expect(page.get_by_text("AI 推定，待人工检查", exact=False)).to_be_visible()
    page.locator('input[name="title"]').fill("题目自动课程")
    page.get_by_role("button", name="创建课程项目", exact=True).click()
    project_id = int(page.url.rsplit("/", 1)[1])
    job_id = generate(page, "course_draft_generate")
    assert site.jobs.get(job_id).state == "queued"
    model = site.run_worker([site.outline(10)] + [BODY] * 10)
    page.get_by_role("link", name="手动刷新任务", exact=True).click()
    expect(page.get_by_text("生成完成", exact=False)).to_be_visible()
    expect(page.get_by_text("未知费用", exact=False).first).to_be_visible()
    frozen = site.drafts.freeze_input(project_id)
    assert len(frozen.chapters) == 10 and all(c.markdown == BODY for c in frozen.chapters)
    assert not frozen.outline.confirmed
    assert len(model.calls) == 11
    page.goto(f"{site.url}/admin/production/{project_id}")
    expect(page.get_by_text("正文生成或编辑完整；结构检查通过；人工待审核。结构通过不说明事实、来源或教学价值已审核。", exact=True)).to_be_visible()
    expect(page.get_by_text("人工待审核", exact=False).first).to_be_visible()
    precise_id = create_workbench_project(page, site, title="精细课程", mode="precise")
    generate_form = page.locator('form[action$="/generate"]')
    generate_form.locator('select[name="kind"]').select_option("chapters_generate")
    generate_form.get_by_role("button").click()
    expect(page.get_by_text("outline_unconfirmed", exact=True)).to_be_visible()
    page.get_by_role("link", name="明确重新读取页面").click()
    page.get_by_role("button", name="确认当前大纲生成方向", exact=True).click()
    job_id = generate(page, "chapters_generate")
    site.run_worker([BODY])
    assert site.jobs.get(job_id).state == "succeeded"
    assert site.drafts.freeze_input(precise_id).outline.confirmed
    page.goto(f"{site.url}/admin/production/{precise_id}")
    page.locator('textarea[name="goal"]').fill("变更目标")
    page.get_by_role("button", name="保存需求", exact=True).click()
    assert not site.drafts.freeze_input(precise_id).outline.confirmed
    form = page.locator('form[action$="/generate"]')
    form.locator('select[name="kind"]').select_option("chapters_generate")
    form.get_by_role("button").click()
    expect(page.get_by_text("outline_unconfirmed", exact=True)).to_be_visible()
    page.goto(f"{site.url}/admin/production/{project_id}")
    page.get_by_role("button", name="归档项目", exact=True).click()
    page.goto(site.url + "/admin/production")
    expect(page.get_by_role("link", name="题目自动课程", exact=True)).to_have_count(0)
    page.get_by_role("link", name="已归档项目", exact=True).click()
    expect(page.get_by_role("link", name="题目自动课程", exact=True)).to_be_visible()
    assert site.drafts.freeze_input(project_id).chapters[0].markdown == BODY


def test_unsaved_edit_prompt_and_clear_after_successful_save(workbench_page):
    page = workbench_page
    editor_url = page.url
    page.locator('textarea[name="markdown"]').fill(BODY)
    dialogs = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.type), dialog.dismiss()))
    page.get_by_role("link", name="返回项目", exact=True).click()
    assert dialogs == ["beforeunload"]
    assert page.url == editor_url
    page.get_by_role("button", name="保存正文", exact=True).click()
    expect(page.locator('textarea[name="markdown"]')).to_have_value(BODY)
    page.get_by_role("link", name="返回项目", exact=True).click()
    assert len(dialogs) == 1
    assert page.url != editor_url


def test_conflict_preserves_200kib_text_without_localstorage(workbench_page, workbench_context):
    page = workbench_page
    editor_url = page.url
    other = workbench_context.new_page()
    other.goto(editor_url)
    other.locator('textarea[name="markdown"]').fill("服务器版本")
    other.get_by_role("button", name="保存正文", exact=True).click()
    page.bring_to_front()
    unit = "中文😀% <tag>源码</tag> "
    source = unit * (204800 // len(unit.encode()))
    source += "x" * (204800 - len(source.encode()))
    page.locator('textarea[name="markdown"]').fill(source)
    page.get_by_role("button", name="保存正文", exact=True).click()
    expect(page.get_by_role("heading", name="输入未保存", exact=True)).to_be_visible()
    expect(page.locator('textarea[name="markdown"]')).to_have_value(source)
    assert page.locator('input[name="body_revision"]').input_value() == ""
    assert page.locator('form[action$="/save"]').count() == 0
    assert page.evaluate("Object.keys(localStorage)") == []
    dialogs = []
    def dismiss_dialog(dialog):
        dialogs.append(dialog.type)
        dialog.dismiss()
    page.on("dialog", dismiss_dialog)
    page.get_by_role("link", name="明确重新读取页面").click()
    assert dialogs == ["beforeunload"]
    page.remove_listener("dialog", dismiss_dialog)
    page.on("dialog", lambda dialog: dialog.accept())
    page.get_by_role("link", name="明确重新读取页面").click()
    expect(page.locator('textarea[name="markdown"]')).to_have_value("服务器版本")


def test_candidate_is_escaped_and_explicitly_adopted(workbench_page, workbench_site):
    page, site = workbench_page, workbench_site
    page.context.add_init_script("window.__candidateExecuted = 0")
    external = []
    page.on("request", lambda request: external.append(request.url)
            if urlsplit(request.url).hostname == "external.invalid" else None)
    editor_url = page.url
    project_id = int(editor_url.split("/production/")[1].split("/")[0])
    chapter_id = editor_url.rsplit("/", 1)[1]
    page.locator('textarea[name="markdown"]').fill(BODY)
    page.get_by_role("button", name="保存正文", exact=True).click()
    page.get_by_role("button", name="重新生成本章候选", exact=True).click()
    site.run_worker([BODY + PROBE])
    page.get_by_role("link", name="手动刷新任务", exact=True).click()
    page.get_by_role("link", name="查看生成候选", exact=True).click()
    expect(page.locator('textarea[name="candidate_text"]')).to_have_value(BODY + PROBE)
    expect(page.locator('textarea[name="current_markdown"]')).to_have_value(BODY)
    expect(page.locator(".candidate-preview")).to_be_visible()
    assert page.evaluate("window.__candidateExecuted") == 0
    assert external == []
    assert page.locator(".candidate-preview img, .candidate-preview script, .candidate-preview iframe").count() == 0
    link = page.locator('.candidate-preview a[href="https://external.invalid/link"]')
    expect(link).to_have_attribute("rel", "noopener noreferrer")
    assert site.drafts.freeze_input(project_id).chapters[0].markdown == BODY.replace("\n", "\r\n")
    page.get_by_role("button", name="采用章节候选", exact=True).click()
    assert site.drafts.freeze_input(project_id).chapters[0].markdown == BODY + PROBE
    page.goto(f"{site.url}/admin/production/{project_id}/preview")
    assert page.evaluate("window.__candidateExecuted") == 0 and external == []
    page.goto(editor_url)
    page.get_by_role("button", name="重新生成本章候选", exact=True).click()
    site.run_worker(["拒绝的候选"])
    page.get_by_role("link", name="手动刷新任务", exact=True).click()
    page.get_by_role("link", name="查看生成候选", exact=True).click()
    page.get_by_role("button", name="拒绝候选", exact=True).click()
    assert site.drafts.freeze_input(project_id).chapters[0].id == chapter_id
    assert site.drafts.freeze_input(project_id).chapters[0].markdown == BODY + PROBE


def test_nojs_native_save_preview_and_task_refresh(real_browser, workbench_site):
    site = workbench_site
    context = real_browser.new_context(java_script_enabled=False, viewport={"width": 390, "height": 844})
    context.route("**/*", lambda route: route.continue_() if urlsplit(route.request.url).hostname == "127.0.0.1" else route.abort())
    try:
        page = context.new_page()
        login_workbench(page, site)
        project_id = create_workbench_project(page, site)
        page.get_by_role("link", name="章节1", exact=True).click()
        expect(page.get_by_text("无 JavaScript 时没有离开提醒", exact=False)).to_be_visible()
        page.locator('textarea[name="markdown"]').fill(BODY)
        page.get_by_role("button", name="保存正文", exact=True).click()
        expect(page.locator(".production-preview")).to_contain_text("复盘")
        page.get_by_role("link", name="返回项目", exact=True).click()
        job_id = generate(page, "draft_validate")
        site.run_worker([])
        page.get_by_role("link", name="手动刷新任务", exact=True).click()
        expect(page.get_by_text("生成完成", exact=False)).to_be_visible()
        from datetime import datetime, timedelta, timezone
        from course_platform.database import transaction
        with transaction(site.db) as conn:
            raw_time = conn.execute("SELECT created_at FROM production_jobs WHERE id=?", (job_id,)).fetchone()[0]
        expected_time = datetime.fromisoformat(raw_time).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        expect(page.locator("time").first).to_contain_text(expected_time + " Asia/Shanghai")
        expect(page.locator("time").first).to_have_attribute("datetime", raw_time)
        page.goto(f"{site.url}/admin/production/{project_id}/preview")
        expect(page.get_by_role("heading", name="安全预览", exact=True)).to_be_visible()
        expect(page.get_by_text("复盘", exact=True)).to_be_visible()
    finally:
        context.close()


def test_narrow_editor_scrolls_locally_and_actions_are_reachable(workbench_page):
    page = workbench_page
    page.set_viewport_size({"width": 390, "height": 844})
    source = BODY + "\n```\n" + "long_code_" * 100 + "\n```\n\n|" + "列|" * 15 + "\n|" + "---|" * 15 + "\n|" + "中文内容|" * 15
    page.locator('textarea[name="markdown"]').fill(source)
    page.get_by_role("button", name="保存正文", exact=True).click()
    expect(page.locator('.editor-columns')).to_be_visible()
    dimensions = page.evaluate("""() => ({width:innerWidth, document:document.documentElement.scrollWidth,
      columns:getComputedStyle(document.querySelector('.editor-columns')).gridTemplateColumns.split(' ').length,
      code:[...document.querySelectorAll('.production-preview pre')].map(e=>[e.clientWidth,e.scrollWidth]),
      tables:[...document.querySelectorAll('.production-preview .table-scroll')].map(e=>[e.clientWidth,e.scrollWidth])})""")
    assert dimensions["document"] <= dimensions["width"]
    assert dimensions["columns"] == 1
    assert any(scroll > width for width, scroll in dimensions["code"])
    assert any(scroll > width for width, scroll in dimensions["tables"])
    page.get_by_role("button", name="保存正文", exact=True).scroll_into_view_if_needed()
    assert page.get_by_role("button", name="保存正文", exact=True).bounding_box()["x"] >= 0
    assert page.evaluate("Object.keys(localStorage)") == []


def test_60_chapter_budget_refusal_has_next_step(workbench_context, workbench_site):
    page, site = workbench_context.new_page(), workbench_site
    login_workbench(page, site)
    page.goto(site.url + "/admin/production/new")
    page.locator('input[name="title"]').fill("六十章课程")
    page.locator('input[name="chapter_count"]').fill("60")
    page.get_by_role("button", name="创建课程项目", exact=True).click()
    project_id = int(page.url.rsplit("/", 1)[1])
    form = page.locator('form[action$="/generate"]')
    form.get_by_role("button").click()
    expect(page.get_by_text("token_budget_exceeded", exact=True)).to_be_visible()
    expect(page.get_by_text("减少章节数或参考输入", exact=False)).to_be_visible()
    expect(page.get_by_text("联系维护者调整额度", exact=False)).to_be_visible()
    assert site.projects.get(project_id).brief.chapter_count == 60
    assert site.config.task_token_limit == 200000
    assert site.jobs.list_jobs()[0] == []


@pytest.mark.parametrize("nojs", [False, True], ids=["chrome", "nojs"])
def test_owner_repairs_structural_findings_without_rewriting_history(real_browser, workbench_site, nojs):
    from course_platform.database import transaction
    site = workbench_site
    context = real_browser.new_context(java_script_enabled=not nojs, viewport={"width": 390, "height": 844})
    context.route("**/*", lambda route: route.continue_() if urlsplit(route.request.url).hostname == "127.0.0.1" else route.abort())
    try:
        page = context.new_page()
        login_workbench(page, site)
        project_id = create_workbench_project(page, site)
        chapter = site.drafts.freeze_input(project_id).chapters[0]
        base = f"{site.url}/admin/production/{project_id}"
        page.goto(base + "/chapters/" + chapter.id)
        page.locator('textarea[name="markdown"]').fill(BODY.split("## 总结")[0] + "## 总结\n")
        page.get_by_role("button", name="保存正文", exact=True).click()
        page.get_by_role("link", name="返回项目", exact=True).click()
        page.get_by_role("button", name="记录草稿检查", exact=True).click()
        with transaction(site.db) as conn:
            recorded = tuple(conn.execute("SELECT * FROM draft_checks ORDER BY id DESC LIMIT 1").fetchone())
        current = page.locator('[data-findings="current"]')
        expect(current).to_contain_text("缺少总结")
        expect(current).to_contain_text("阻断项")
        repair = current.get_by_role("link", name="编辑此章", exact=True)
        expect(repair).to_have_attribute("href", f"/admin/production/{project_id}/chapters/{chapter.id}")
        repair.click()
        assert page.url == base + "/chapters/" + chapter.id
        expect(page.locator('[data-findings="current"]')).to_contain_text("缺少总结")
        page.locator('textarea[name="markdown"]').fill(BODY)
        page.get_by_role("button", name="保存正文", exact=True).click()
        for path in (base, base + "/preview", base + "/chapters/" + chapter.id):
            page.goto(path)
            expect(page.locator('[data-findings="current"]')).not_to_contain_text("missing_summary")
            expect(page.locator('[data-findings="recorded"]')).to_contain_text("missing_summary")
            expect(page.locator('[data-findings="recorded"]')).to_contain_text("已过时")
        with transaction(site.db) as conn:
            assert tuple(conn.execute("SELECT * FROM draft_checks ORDER BY id DESC LIMIT 1").fetchone()) == recorded
    finally:
        context.close()


def test_narrow_nojs_snapshot_contains_complete_locally_scrollable_raw_sources(real_browser, workbench_site):
    from course_platform.production.models import ReferenceInput
    from course_platform.production.snapshots import SnapshotService
    site = workbench_site
    context = real_browser.new_context(java_script_enabled=False, viewport={"width": 390, "height": 844})
    context.route("**/*", lambda route: route.continue_() if urlsplit(route.request.url).hostname == "127.0.0.1" else route.abort())
    try:
        page = context.new_page()
        login_workbench(page, site)
        project_id = create_workbench_project(page, site)
        reference = "R" * 19970 + "<script>raw()</script>"
        note = "N" * 950
        site.projects.save_reference(site.actor, project_id, site.projects.get(project_id).revision, None,
                                     ReferenceInput(text=reference, source_note=note))
        chapter = site.drafts.freeze_input(project_id).chapters[0]
        source = BODY + "\n```\n" + "long_code_" * 1500 + "<script>raw()</script>\n```"
        site.drafts.save_chapter(site.actor, project_id, chapter.id, None, source)
        site.drafts.acknowledge_changes(site.actor, project_id, site.projects.get(project_id).revision, (chapter.id,))
        snapshot = SnapshotService(site.db, site.drafts).create(site.actor, project_id, site.projects.get(project_id).revision)
        page.goto(f"{site.url}/admin/production/{project_id}/snapshots/{snapshot.id}")
        page.get_by_text("完整冻结交接数据（含渲染与提示词版本）", exact=True).click()
        dimensions = page.evaluate("""() => ({width:innerWidth, document:document.documentElement.scrollWidth,
          columns:getComputedStyle(document.querySelector('.production-columns')).gridTemplateColumns.split(' ').length,
          raw:[...document.querySelectorAll('main pre')].map(e=>({text:e.textContent,width:e.clientWidth,
              scroll:e.scrollWidth,overflow:getComputedStyle(e).overflowX}))})""")
        assert dimensions["document"] <= dimensions["width"], "Raw snapshot source expands the viewport"
        assert dimensions["columns"] == 1
        for expected in (reference, note, source, snapshot.payload_json):
            raw = next(item for item in dimensions["raw"] if item["text"] == expected)
            assert raw["scroll"] > raw["width"] and raw["overflow"] == "auto"
        assert page.locator("main script, main img, main iframe").count() == 0
    finally:
        context.close()
