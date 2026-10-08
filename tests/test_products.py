"""Sale promises, inspection identity, revisions and immutable issued links."""

import json
import zipfile

import pytest
from pydantic import ValidationError

from course_platform.database import transaction
from course_platform.domain import Actor, BusinessError
from course_platform.operations.products import AccessPolicy, SalesChannel, SalesChecklist


CHECKS = SalesChecklist(quality=True, sources=True, ai=True, mobile=True, downloads=True)


@pytest.mark.parametrize("mode,days", [("days", 1), ("days", 3650), ("no_fixed_expiry", None)])
def test_sale_requires_explicit_policy_and_matching_approval(
        product_service, product_owner, published_product_data, db_path, fixture_package, mode, days):
    draft = product_service.create(product_owner, published_product_data.model_copy(update={"policy": None}))
    with pytest.raises(BusinessError) as err:
        product_service.activate(product_owner, draft.id, draft.revision, CHECKS)
    assert err.value.status_code == 409
    policy = AccessPolicy(access_mode=mode, access_days=days, online=True, pdf=False,
                          zip=False, update_policy="current_version")
    draft = product_service.update(product_owner, draft.id, draft.revision,
                                   draft.data.model_copy(update={"policy": policy}))
    active = product_service.activate(product_owner, draft.id, draft.revision, CHECKS)
    with transaction(db_path) as connection:
        issued = product_service.require_sale_ready_in_tx(connection, active.id)
        assert issued.access.access_days == days
        assert issued.version == "0.1.0" and issued.course_slug == "fixture-course"
    with transaction(db_path) as connection:
        approval = connection.execute("SELECT sales_check_json FROM products WHERE id=?", (active.id,)).fetchone()[0]
        assert len(json.loads(approval)["promise_hash"]) == 64
    (fixture_package / "SOURCES.txt").write_text("changed source", encoding="utf-8")
    with transaction(db_path) as connection, pytest.raises(BusinessError):
        product_service.require_sale_ready_in_tx(connection, active.id)


@pytest.mark.parametrize("change", ["outcomes", "pdf", "access_days", "support_text", "channels"])
def test_changed_promises_invalidate_approval(product_service, product_owner, active_product, db_path, change):
    updates = {"outcomes": ["新的成果"], "support_text": "新的支持", "channels": []}
    if change in ("pdf", "access_days"):
        data = active_product.data.model_copy(update={"policy": active_product.data.policy.model_copy(
            update={change: True if change == "pdf" else 31})})
    else:
        data = active_product.data.model_copy(update={change: updates[change]})
    product_service.update(product_owner, active_product.id, active_product.revision, data)
    with transaction(db_path) as connection, pytest.raises(BusinessError):
        product_service.require_sale_ready_in_tx(connection, active_product.id)


def test_stale_product_revision_returns_conflict(product_service, active_product, actor):
    data = active_product.data.model_copy(update={"title": "新的商品标题"})
    updated = product_service.update(actor, active_product.id, active_product.revision, data)
    with pytest.raises(BusinessError) as err:
        product_service.update(actor, active_product.id, active_product.revision, data)
    assert err.value.status_code == 409
    assert updated.revision == active_product.revision + 1
    assert product_service.get_product(updated.id) == updated


def test_paused_product_preserves_issued_snapshots(product_service, product_owner, active_product, db_path):
    with transaction(db_path) as connection:
        issued = product_service.require_sale_ready_in_tx(connection, active_product.id)
        connection.execute("""INSERT INTO access_codes
            (course_id, code_hash, created_at, product_id, access_days, update_policy, course_version, package_hash)
            VALUES (?, ?, '2026-10-02', ?, 30, 'current_version', ?, ?)""",
            (issued.course_id, "a" * 64, issued.product_id, issued.version, issued.package_hash))
        connection.execute("""INSERT INTO entitlements
            (course_id, product_id, source_code_id, course_version, package_hash, access_days)
            VALUES (?, ?, 1, ?, ?, 30)""",
            (issued.course_id, issued.product_id, issued.version, issued.package_hash))
        before = [tuple(row) for row in connection.execute("SELECT * FROM access_codes")]
        rights = [tuple(row) for row in connection.execute("SELECT * FROM entitlements")]
    paused = product_service.set_status(product_owner, active_product.id, active_product.revision, "paused")
    with transaction(db_path) as connection, pytest.raises(BusinessError):
        product_service.require_sale_ready_in_tx(connection, paused.id)
    with pytest.raises(BusinessError) as err:
        product_service.update(product_owner, paused.id, paused.revision,
                               paused.data.model_copy(update={"course_id": None}))
    assert err.value.code == "course_binding_locked"
    restored = product_service.activate(product_owner, paused.id, paused.revision, CHECKS)
    assert restored.status == "active"
    edited = product_service.update(product_owner, restored.id, restored.revision,
        restored.data.model_copy(update={"title": "更新的商品文案", "support_text": "新的支持渠道",
            "policy": restored.data.policy.model_copy(update={"access_days": 3650})}))
    assert edited.data.policy.access_days == 3650
    with transaction(db_path) as connection:
        assert [tuple(row) for row in connection.execute("SELECT * FROM access_codes")] == before
        assert [tuple(row) for row in connection.execute("SELECT * FROM entitlements")] == rights


def test_category_rename_does_not_change_course_id(product_service, product_owner, active_product, db_path, fixture_package):
    product_service.save_category(product_owner, active_product.data.category_id, 1, "renamed", "重命名", 10, False)
    with transaction(db_path) as connection:
        assert tuple(connection.execute("SELECT course_id, slug, content_path FROM courses").fetchone()) == (
            "fixture-course", "fixture-course", str(fixture_package.resolve()))
        assert connection.execute("SELECT course_id FROM products").fetchone()[0] == "fixture-course"
    with transaction(db_path) as connection, pytest.raises(BusinessError):
        product_service.require_sale_ready_in_tx(connection, active_product.id)
    with pytest.raises(BusinessError) as err:
        product_service.save_category(product_owner, active_product.data.category_id, 1, "technical", "旧表单", 0, True)
    assert err.value.status_code == 409


def test_product_listing_has_twenty_items_per_page(product_service, product_owner, published_product_data):
    for index in range(21):
        product_service.create(product_owner, published_product_data.model_copy(
            update={"course_id": None, "title": f"课程 {index:02}"}))
    first, total = product_service.list_products(status="draft", category_id=published_product_data.category_id,
                                                title="课程", page=1)
    second, _ = product_service.list_products(status=None, category_id=None, title="", page=2)
    assert len(first) == 20 and total == 21 and len(second) == 1
    assert not {record.id for record in first} & {record.id for record in second}
    assert product_service.list_products(status="active", category_id=None, title="", page=1) == ([], 0)
    assert product_service.list_products(status=None, category_id=None, title="%", page=1) == ([], 0)


@pytest.mark.parametrize("changes", [{"access_days": 0}, {"access_days": 3651}, {"access_days": True},
    {"access_days": "1"}, {"access_mode": "no_fixed_expiry", "access_days": 1},
    {"access_days": None}, {"online": "true"}, {"update_policy": "future"}, {"extra": 1}])
def test_access_policy_is_strict(changes):
    values = dict(access_mode="days", access_days=30, online=True, pdf=False, zip=False,
                  update_policy="current_version")
    with pytest.raises(ValidationError):
        AccessPolicy(**(values | changes))


@pytest.mark.parametrize("url", ["javascript:alert(1)", "http://shop.example", "//shop.example", "https://u:p@shop.example",
    "https://shop.example/\nfoo", "https://shop.example/\\foo"])
def test_sales_channels_require_https(url):
    with pytest.raises(ValidationError):
        SalesChannel(name="店铺", url=url)


@pytest.mark.parametrize("fault", ["checklist", "pdf", "zip", "no_formats", "db_unpublished", "manifest_unpublished",
    "identity", "version", "hash", "unsafe"])
def test_activation_rejects_unready_packages(product_service, product_owner, published_product_data,
                                             fixture_package, db_path, fault):
    data = published_product_data
    checks = CHECKS
    if fault in ("pdf", "zip"):
        data = data.model_copy(update={"policy": data.policy.model_copy(update={fault: True})})
    if fault == "no_formats":
        data = data.model_copy(update={"policy": data.policy.model_copy(update={"online": False})})
    if fault == "checklist":
        checks = checks.model_copy(update={"mobile": False})
    if fault == "manifest_unpublished":
        manifest = json.loads((fixture_package / "manifest.json").read_text(encoding="utf-8"))
        manifest["status"] = "draft"
        (fixture_package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if fault in ("db_unpublished", "identity", "version", "hash"):
        column, value = {"db_unpublished": ("status", "draft"), "identity": ("slug", "wrong"),
                         "version": ("version", "9.0.0"), "hash": ("package_hash", "b" * 64)}[fault]
        with transaction(db_path) as connection:
            connection.execute(f"UPDATE courses SET {column}=?", (value,))
    if fault == "unsafe":
        (fixture_package / "chapters/01.html").write_text("<script>unsafe()</script>", encoding="utf-8")
    draft = product_service.create(product_owner, data)
    with pytest.raises(BusinessError) as err:
        product_service.activate(product_owner, draft.id, draft.revision, checks)
    assert err.value.status_code == 409
    assert product_service.get_product(draft.id).status == "draft"


def test_import_draft_uses_existing_transaction_without_overwriting(product_service, product_owner,
                                                                    published_product_data, db_path):
    with transaction(db_path) as connection:
        draft = product_service.ensure_draft_in_tx(connection, "fixture-course")
        assert draft.status == "draft" and draft.data.policy is None
        same = product_service.ensure_draft_in_tx(connection, "fixture-course")
        assert same == draft
    edited = product_service.update(product_owner, draft.id, draft.revision,
                                    draft.data.model_copy(update={"title": "人工文案"}))
    with transaction(db_path) as connection:
        assert product_service.ensure_draft_in_tx(connection, "fixture-course") == edited


def test_domain_denial_commits_once_after_rollback(product_service, product_owner, active_product, db_path):
    with transaction(db_path) as connection:
        last = connection.execute("SELECT max(id) FROM admin_events").fetchone()[0]
    with pytest.raises(BusinessError) as err:
        product_service.update(Actor(1, "domain-request"), active_product.id, 0, active_product.data)
    assert err.value.denial_recorded is True
    with transaction(db_path) as connection:
        events = [dict(row) for row in connection.execute("SELECT * FROM admin_events WHERE id>?", (last,))]
        assert connection.execute("SELECT revision FROM products").fetchone()[0] == active_product.revision
    assert len(events) == 1
    assert events[0]["request_id"] == "domain-request"
    assert json.loads(events[0]["changes_json"]) == {"error_code": "stale_revision"}


@pytest.mark.parametrize("slug", ["../bad", "A B", "a/b", "x<script>"])
def test_categories_require_safe_slug(product_service, product_owner, slug):
    with pytest.raises(BusinessError) as err:
        product_service.save_category(product_owner, None, None, slug, "分类", 0, True)
    assert err.value.status_code == 400


@pytest.mark.parametrize("column,value", [("title", "wrong"), ("access_days", 99),
                                          ("update_policy", None), ("course_id", None)])
def test_sale_rejects_desynchronized_storage(product_service, product_owner, published_product_data, db_path, column, value):
    # Storage used by downstream issuance must agree with the approved promises.
    draft = product_service.create(product_owner, published_product_data)
    with transaction(db_path) as connection:
        connection.execute(f"UPDATE products SET {column}=? WHERE id=?", (value, draft.id))
    with pytest.raises(BusinessError) as err:
        product_service.activate(product_owner, draft.id, draft.revision, CHECKS)
    assert err.value.code == "product_invalid"


def test_real_pdf_and_zip_are_required_for_promised_formats(
        product_service, product_owner, published_product_data, db_path, fixture_package):
    from course_platform.content_inspection import inspect_package

    downloads = fixture_package / "downloads"
    downloads.mkdir()
    (downloads / "course.pdf").write_bytes(b"%PDF-1.7\nfixture")
    with zipfile.ZipFile(downloads / "course.zip", "w") as archive:
        for path in ("manifest.json", "index.html", "SOURCES.txt", "LICENSE.txt", "chapters/01.html"):
            archive.write(fixture_package / path, path)
    inspection = inspect_package(fixture_package)
    with transaction(db_path) as connection:
        connection.execute("UPDATE courses SET package_hash=?", (inspection.fingerprint,))
    data = published_product_data.model_copy(update={"policy": published_product_data.policy.model_copy(
        update={"online": False, "pdf": True, "zip": True})})
    draft = product_service.create(product_owner, data)
    active = product_service.activate(product_owner, draft.id, draft.revision, CHECKS)
    with transaction(db_path) as connection:
        issued = product_service.require_sale_ready_in_tx(connection, active.id)
    assert issued.access.pdf and issued.access.zip and not issued.access.online
    (downloads / "course.pdf").unlink()
    with transaction(db_path) as connection, pytest.raises(BusinessError):
        product_service.require_sale_ready_in_tx(connection, active.id)


def test_republished_package_needs_fresh_approval(product_service, product_owner, active_product,
                                                  db_path, fixture_package):
    from course_platform.content_inspection import inspect_package

    (fixture_package / "SOURCES.txt").write_text("reviewed updated sources", encoding="utf-8")
    with transaction(db_path) as connection:
        connection.execute("UPDATE courses SET package_hash=?", (inspect_package(fixture_package).fingerprint,))
    with transaction(db_path) as connection, pytest.raises(BusinessError) as err:
        product_service.require_sale_ready_in_tx(connection, active_product.id)
    assert err.value.code == "sales_approval_stale"
    renewed = product_service.activate(product_owner, active_product.id, active_product.revision, CHECKS)
    with transaction(db_path) as connection:
        assert product_service.require_sale_ready_in_tx(connection, renewed.id).course_id == "fixture-course"


def test_unbound_draft_can_select_course_but_duplicate_binding_is_rejected(
        product_service, product_owner, published_product_data):
    draft = product_service.create(product_owner, published_product_data.model_copy(update={"course_id": None}))
    bound = product_service.update(product_owner, draft.id, draft.revision, published_product_data)
    assert bound.data.course_id == "fixture-course"
    with pytest.raises(BusinessError) as err:
        product_service.create(product_owner, published_product_data)
    assert err.value.code == "product_conflict"


def test_archived_product_cannot_reactivate(product_service, product_owner, active_product):
    archived = product_service.set_status(product_owner, active_product.id, active_product.revision, "archived")
    with pytest.raises(BusinessError) as err:
        product_service.activate(product_owner, archived.id, archived.revision, CHECKS)
    assert err.value.code == "product_archived"


def test_success_audit_failure_rolls_back_product_write(product_service, product_owner, published_product_data,
                                                       db_path, monkeypatch):
    from course_platform.operations import products

    original = products.append_event

    def reject_success(connection, event):
        if event.outcome == "success":
            raise BusinessError("audit_payload", "Audit metadata unavailable.", 400)
        return original(connection, event)

    monkeypatch.setattr(products, "append_event", reject_success)
    with pytest.raises(BusinessError):
        product_service.create(product_owner, published_product_data)
    with transaction(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM products").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM admin_events WHERE action='product.create' AND outcome='denied'").fetchone()[0] == 1


def test_category_conflict_preserves_first_record(product_service, product_owner, published_product_data):
    with pytest.raises(BusinessError) as err:
        product_service.save_category(product_owner, None, None, "technical", "不覆盖", 0, True)
    assert err.value.status_code == 409
    assert product_service.list_categories()[0]["name"] == "技术"


def test_category_order_and_disable_preserve_references(product_service, product_owner, active_product):
    second = product_service.save_category(product_owner, None, None, "second", "第二类", -10, True)
    product_service.save_category(product_owner, active_product.data.category_id, 1, "technical", "技术", 5, False)
    categories = product_service.list_categories()
    assert [category["id"] for category in categories] == [second, active_product.data.category_id]
    assert categories[1]["enabled"] == 0 and categories[1]["revision"] == 2
    assert product_service.get_product(active_product.id).data.category_id == active_product.data.category_id


def test_unbound_draft_can_be_archived(product_service, product_owner, published_product_data, db_path):
    draft = product_service.create(product_owner, published_product_data.model_copy(update={"course_id": None}))
    archived = product_service.set_status(product_owner, draft.id, draft.revision, "archived")
    assert archived.status == "archived" and archived.data.course_id is None
    assert archived.revision == draft.revision + 1
    with transaction(db_path) as connection, pytest.raises(BusinessError) as err:
        product_service.require_sale_ready_in_tx(connection, archived.id)
    assert err.value.code == "product_not_active"


def test_category_management_has_twenty_items_per_page(product_service, product_owner):
    for index in range(21):
        product_service.save_category(product_owner, None, None, f"category-{index}", f"分类 {index}", index, True)
    first, total = product_service.list_category_page(page=1)
    second, second_total = product_service.list_category_page(page=2)
    assert total == second_total == 21
    assert [row["name"] for row in first] == [f"分类 {index}" for index in range(20)]
    assert [row["name"] for row in second] == ["分类 20"]
    assert len(product_service.list_categories()) == 21  # complete selector lookup


def test_auto_import_before_owner_is_atomic_and_honestly_attributed(product_service, db_path, fixture_package):
    from course_platform.content import CourseManifest
    from course_platform.database import sync_course

    manifest = CourseManifest.model_validate_json((fixture_package / "manifest.json").read_bytes())
    sync_course(manifest, fixture_package, db_path)
    with pytest.raises(RuntimeError, match="import rollback"):
        with transaction(db_path) as connection:
            product_service.ensure_draft_in_tx(connection, manifest.course_id)
            raise RuntimeError("import rollback")
    with transaction(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM products").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM categories").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM admin_events").fetchone()[0] == 0
        draft = product_service.ensure_draft_in_tx(connection, manifest.course_id)
        same = product_service.ensure_draft_in_tx(connection, manifest.course_id)
        assert draft == same and draft.status == "draft" and draft.data.policy is None
        assert connection.execute("SELECT count(*) FROM admins").fetchone()[0] == 0
        assert connection.execute("SELECT created_by FROM products").fetchone()[0] is None
        assert connection.execute("SELECT created_by FROM categories").fetchone()[0] is None
        assert connection.execute("SELECT access_days, update_policy FROM products").fetchone()[:] == (None, None)
        assert json.loads(connection.execute("SELECT description FROM products").fetchone()[0])["policy"] is None
        events = [dict(row) for row in connection.execute("SELECT * FROM admin_events")]
    assert [row["action"] for row in events] == ["category.create", "product.create"]
    assert all(row["actor_admin_id"] is None for row in events)
    assert len({row["request_id"] for row in events}) == 1


def test_import_requires_caller_transaction_and_existing_course(product_service, db_path):
    from contextlib import closing
    from course_platform.database import connect

    with closing(connect(db_path)) as connection, pytest.raises(BusinessError) as err:
        product_service.ensure_draft_in_tx(connection, "missing")
    assert err.value.code == "import_transaction"
    with transaction(db_path) as connection, pytest.raises(BusinessError) as err:
        product_service.ensure_draft_in_tx(connection, "missing")
    assert err.value.code == "course_missing"


@pytest.mark.parametrize("page", [0, -1, True, 999999999999999999])
def test_shared_pagination_rejects_unsafe_pages(product_service, page):
    for listing in (lambda: product_service.list_products(status=None, category_id=None, title="", page=page),
                    lambda: product_service.list_category_page(page=page)):
        with pytest.raises(BusinessError) as err:
            listing()
        assert err.value.status_code == 400


@pytest.mark.parametrize("identity", ["unknown", "disabled"])
def test_nullable_import_attribution_does_not_authorize_manual_writes(
        product_service, product_owner, published_product_data, db_path, identity):
    if identity == "disabled":
        with transaction(db_path) as connection:
            connection.execute("UPDATE admins SET enabled=0 WHERE id=?", (product_owner.admin_id,))
        actor = product_owner
    else:
        actor = Actor(999, "unknown-owner-request")
    for mutation in (
            lambda: product_service.create(actor, published_product_data),
            lambda: product_service.save_category(actor, None, None, "manual", "Manual", 0, True)):
        with pytest.raises(BusinessError) as err:
            mutation()
        assert err.value.code == "owner_required" and err.value.status_code == 403
        assert err.value.denial_recorded
    with transaction(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM products").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM categories WHERE slug='manual'").fetchone()[0] == 0
        denials = connection.execute("""SELECT actor_admin_id, action, outcome, request_id
            FROM admin_events WHERE action IN ('product.create', 'category.create') AND outcome='denied'
            ORDER BY id""").fetchall()
    assert [row[1] for row in denials] == ["product.create", "category.create"]
    assert all(row[0] == (product_owner.admin_id if identity == "disabled" else None)
               and row[2] == "denied" and row[3] == actor.request_id for row in denials)
