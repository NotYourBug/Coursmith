"""Real owner and buyer UI, without daily CLI or manufactured verification."""
import csv
from contextlib import closing
from datetime import datetime, timezone
import io
import re

import pytest
from playwright.sync_api import expect

from course_platform.database import open_readonly, transaction

pytestmark = pytest.mark.browser


def login(page, site):
    origins = []
    def record_origin(request):
        if request.method == "POST" and request.url.endswith("/admin/login"):
            origins.append(request.headers.get("origin"))
    page.on("request", record_origin)
    page.goto(site.url + "/admin/login")
    page.get_by_label("账号", exact=True).fill("owner")
    page.get_by_label("密码", exact=True).fill("example-pass-123")
    page.get_by_role("button", name="登录", exact=True).click()
    page.remove_listener("request", record_origin)
    assert origins == [site.origin], f"Genuine browser login Origin: {origins}"
    expect(page).to_have_url(site.url + "/admin")


def prepare_product(page, site, *, online=True, pdf=False, zip=False):
    login(page, site)
    page.goto(site.url + "/admin/categories")
    form = page.locator('form[action="/admin/categories"]')
    form.locator('[name="slug"]').fill("operator-technical")
    form.locator('[name="name"]').fill("技术")
    form.get_by_role("button", name="创建分类").click()
    expect(page).to_have_url(site.url + "/admin/categories")
    page.goto(site.url + "/admin/products/1")
    form = page.locator('form[action="/admin/products/1"]')
    values = dict(title="真实验收课程", synopsis="移动阅读练习", audience="初学者", prerequisites="无",
                  outcomes="完成章节练习", ai_disclosure="AI 辅助且经营者已审查", support_text="联系原购买店铺",
                  channels="店铺|https://shop.example/course", access_days="30")
    for name, value in values.items():
        form.locator(f'[name="{name}"]').fill(value)
    form.locator('[name="category_id"]').select_option(index=0)
    form.locator('[name="access_mode"]').select_option("days")
    for name, value in dict(online=online, pdf=pdf, zip=zip).items():
        form.locator(f'[name="{name}"]').set_checked(value)
    form.get_by_role("button", name="保存商品").click()
    # Actual readiness checks inspect the fixture package; the operator affirms
    # review in the UI, with live public preview exercised immediately below.
    page.goto(site.url + "/admin/products/1")
    form = page.locator('form[action$="/activate"]')
    for name in ("quality", "sources", "ai", "mobile", "downloads"):
        form.locator(f'[name="{name}"]').check()
    form.get_by_role("button", name="重新检查并启用").click()
    expect(page.get_by_role("status")).to_contain_text("销售检查有效")


def issue_order(page, site):
    page.goto(site.url + "/admin/orders/new")
    for name, value in dict(channel="店铺", shop_id="acceptance-shop", external_order_id="task12-private-order",
                            paid_cents="100", paid_at=datetime.now(timezone.utc).isoformat(),
                            note="=核验付款及原购买者").items():
        page.locator(f'[name="{name}"]').fill(value)
    page.locator('[name="confirm"]').check()
    page.get_by_role("button", name="登记", exact=True).click()
    page.get_by_role("button", name="按登记政策发码").click()
    expect(page.locator("#issued-code-table")).to_be_visible()
    return page.locator("#issued-code-table tbody td").nth(1).inner_text()


def redeem_ui(page, site, code):
    page.goto(site.url + "/access")
    page.get_by_label("激活码", exact=True).fill(code)
    page.get_by_role("button", name="兑换并领取学习凭证").click()
    expect(page.get_by_role("heading", name="访问已就绪")).to_be_visible()
    return page.locator("code").inner_text()


def restore_ui(page, site, key, *, success=True):
    page.goto(site.url + "/access/restore")
    page.get_by_label("学习凭证", exact=True).fill(key)
    page.get_by_role("button", name="恢复访问").click()
    if success:
        expect(page.get_by_role("heading", name="访问已就绪")).to_be_visible()
        assert key not in page.content()


def business_rows(site):
    with closing(open_readonly(site.db)) as connection:
        return {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                for table in ("orders", "entitlements", "entitlement_progress")}


def assert_nested_render(page, url):
    response = page.goto(url)
    assert response.status == 200
    expect(page.locator("h1")).to_have_css("color", "rgb(12, 34, 56)")
    expect(page.locator("img")).to_be_visible()
    assert page.locator("img").evaluate("img => img.complete && img.naturalWidth === 1")
    expected_asset = ("/learn/fixture-course/assets/free.png" if "/learn/" in url
                      else "/courses/fixture-course/assets/free.png")
    assert expected_asset in page.locator(".image-set").evaluate("e => getComputedStyle(e).backgroundImage")
    paths = page.locator("svg > path")
    assert paths.count() == 2
    assert paths.nth(0).bounding_box()["width"] == 20
    assert paths.nth(1).bounding_box()["x"] - paths.nth(0).bounding_box()["x"] == 20
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_owner_and_buyer_complete_delivery_without_daily_cli(real_browser, live_site):
    site = live_site
    contexts = []
    def device(**kwargs):
        context = real_browser.new_context(viewport={"width": 390, "height": 844}, ignore_https_errors=True, **kwargs)
        contexts.append(context)
        return context, context.new_page()
    try:
        owner_context, owner = device(permissions=["clipboard-read", "clipboard-write"])
        prepare_product(owner, site)
        code = issue_order(owner, site)
        prose = owner.locator("textarea[readonly]").input_value()
        assert code in prose and site.url + "/courses/fixture-course" in prose and "30" in prose
        owner.get_by_role("button", name="复制激活码").click()
        expect(owner.locator("#copy-status")).to_contain_text("已复制")
        assert owner.evaluate("navigator.clipboard.readText()") == code
        textarea = owner.locator("textarea[readonly]")
        textarea.focus()
        textarea.press("ControlOrMeta+A")
        textarea.press("ControlOrMeta+C")
        assert owner.evaluate("navigator.clipboard.readText()").replace("\r\n", "\n") == prose
        with owner.expect_download() as event:
            owner.get_by_role("button", name="下载当前 CSV").click()
        download = event.value
        from pathlib import Path
        csv_bytes = Path(download.path()).read_bytes()
        download.delete()
        assert csv_bytes.startswith(b"\xef\xbb\xbf")
        records = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
        assert records[1][1] == code and len(records) == 2
        assert code not in str(owner_context.storage_state())
        assert owner.evaluate("localStorage.length + sessionStorage.length") == 0
        owner.get_by_role("link", name="订单详情", exact=True).click()
        assert code not in owner.content()
        assert owner.locator("#download-csv").count() == 0
        form = owner.locator('form[action$="/confirm-delivery"]')
        form.locator('[name="confirm"]').check()
        form.get_by_role("button", name="确认发货").click()
        buyer_context, buyer = device(java_script_enabled=False)
        key = redeem_ui(buyer, site, code)
        assert re.fullmatch(r"LK-[A-Za-z0-9_-]{43}", key)
        assert key not in str(buyer_context.storage_state())
        assert all(cookie["name"] != "coursmith_admin" for cookie in buyer_context.cookies())
        buyer.goto(site.url + "/learn/fixture-course/chapters/1")
        buyer.get_by_role("button", name="标记完成").click()
        expect(buyer.get_by_role("button", name="取消完成")).to_be_visible()
        before = business_rows(site)
        with closing(open_readonly(site.db)) as c:
            promises = tuple(c.execute("SELECT expires_at,issued_policy_json,verified_at,verified_by,verified_reason FROM entitlements").fetchone())
        _, second = device()
        restore_ui(second, site, key)
        second.goto(site.url + "/learn/fixture-course")
        expect(second.locator("body")).to_contain_text("已完成")
        _, third = device()
        restore_ui(third, site, key)
        _, fourth = device()
        restore_ui(fourth, site, key, success=False)
        expect(fourth.get_by_label("学习凭证", exact=True)).to_have_value("")
        checkbox = fourth.get_by_role("checkbox", name="确认退出最早设备")
        expect(checkbox).not_to_be_checked()
        assert key not in fourth.content() and key not in fourth.url
        fourth.get_by_label("学习凭证", exact=True).fill(key)
        checkbox.check()
        fourth.get_by_role("button", name="恢复访问").click()
        expect(fourth.get_by_role("heading", name="访问已就绪")).to_be_visible()
        assert buyer.goto(site.url + "/learn/fixture-course/chapters/1").status == 403
        for alias in ("chapters/1", "chapters/intro.html", "lessons/unit/intro.html"):
            assert_nested_render(fourth, site.url + "/learn/fixture-course/" + alias)
        # Error-scenario fixture expires an actual independently restored session.
        # Browser access must obey server expiry even if its cookie still exists.
        import hashlib
        session = next(c["value"] for c in second.context.cookies() if c["name"] == "course_session_fixture-course")
        with transaction(site.db) as c:
            c.execute("UPDATE sessions SET expires_at='2026-10-01T00:00:00+00:00' WHERE session_hash=?", (hashlib.sha256(session.encode()).hexdigest(),))
        assert second.goto(site.url + "/learn/fixture-course/chapters/1").status == 403
        owner.goto(site.url + "/admin/entitlements/1")
        form = owner.locator('form[action$="/reset-credential"]')
        form.locator('[name="reason"]').fill("店铺核验实际订单及原购买者")
        form.locator('[name="confirm"]').check()
        form.get_by_role("button", name="确认重置凭证").click()
        new_key = owner.locator("code").inner_text()
        assert new_key != key
        assert second.goto(site.url + "/learn/fixture-course").status == 403
        restore_ui(second, site, key, success=False)
        expect(second.get_by_role("alert")).to_be_visible()
        restore_ui(second, site, new_key)
        second.goto(site.url + "/learn/fixture-course/chapters/1")
        expect(second.get_by_role("button", name="取消完成")).to_be_visible()
        after = business_rows(site)
        assert after["entitlement_progress"] == before["entitlement_progress"]
        # Only revision/credential/session state may change; original promises/proof stay.
        with closing(open_readonly(site.db)) as c:
            assert tuple(c.execute("SELECT expires_at,issued_policy_json,verified_at,verified_by,verified_reason FROM entitlements").fetchone()) == promises
            right = c.execute("SELECT expires_at, verified_at, verified_by, verified_reason FROM entitlements").fetchone()
            assert right[0] and right[1] and right[2] == 1 and right[3]
        owner.goto(site.url + "/admin/entitlements/1")
        assert new_key not in owner.content() and code not in owner.content()
        form = owner.locator('form[action$="/revoke"]')
        form.locator('[name="reason"]').fill("核验后停止验收权益")
        form.locator('[name="confirm"]').check()
        form.get_by_role("button", name="确认撤销权益").click()
        assert second.goto(site.url + "/learn/fixture-course/lessons/unit/intro.html").status == 403
        restore_ui(fourth, site, new_key, success=False)
        expect(fourth.get_by_role("alert")).to_be_visible()
        assert business_rows(site)["entitlement_progress"] == before["entitlement_progress"]
    finally:
        for context in contexts:
            context.close()


def test_mobile_preview_and_no_js_progress(real_browser, mobile_page, live_site):
    site = live_site
    prepare_product(mobile_page, site)
    for alias in ("chapters/1", "chapters/intro.html", "lessons/unit/intro.html"):
        assert_nested_render(mobile_page, site.url + "/courses/fixture-course/" + alias)
    code = issue_order(mobile_page, site)
    context = real_browser.new_context(viewport={"width": 390, "height": 844}, java_script_enabled=False, ignore_https_errors=True)
    try:
        page = context.new_page()
        redeem_ui(page, site, code)
        page.goto(site.url + "/learn/fixture-course/chapters/1")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.get_by_role("button", name="标记完成").click()
        expect(page.get_by_role("button", name="取消完成")).to_be_visible()
        page.get_by_role("button", name="取消完成").click()
        expect(page.get_by_role("button", name="标记完成")).to_be_visible()
        with closing(open_readonly(site.db)) as c:
            assert c.execute("SELECT completed FROM entitlement_progress").fetchone()[0] == 0
    finally:
        context.close()


@pytest.mark.parametrize("format", ["pdf", "zip"])
def test_download_only_original_promise_survives_archive(real_browser, live_site, format):
    context = real_browser.new_context(viewport={"width": 390, "height": 844}, ignore_https_errors=True)
    buyer_context = real_browser.new_context(ignore_https_errors=True)
    try:
        owner = context.new_page()
        prepare_product(owner, live_site, online=False, pdf=format == "pdf", zip=format == "zip")
        code = issue_order(owner, live_site)
        owner.goto(live_site.url + "/admin/products/1")
        owner.get_by_role("button", name="归档商品", exact=True).click()
        buyer = buyer_context.new_page()
        key = redeem_ui(buyer, live_site, code)
        buyer.get_by_role("link", name="进入课程与下载").click()
        link = buyer.locator(f'a[href$="/downloads/course.{format}"]')
        expect(link).to_be_visible()
        with buyer.expect_download() as event:
            link.click()
        from pathlib import Path
        content = Path(event.value.path()).read_bytes()
        event.value.delete()
        assert content.startswith(b"%PDF" if format == "pdf" else b"PK")
        assert buyer.goto(live_site.url + "/learn/fixture-course/chapters/1").status == 403
        restore_ui(buyer, live_site, key)
    finally:
        context.close()
        buyer_context.close()
