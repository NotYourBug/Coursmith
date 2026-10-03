"""Owner product promises and sale approval tied to inspected package bytes."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..audit import AuditEvent, append_event, record_denial
from ..content import CourseManifest
from ..content_inspection import PackageInspection, inspect_package, read_verified_file
from ..database import open_readonly, to_db_time, transaction
from ..domain import Actor, BusinessError, Clock, utc_now


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)


class AccessPolicy(StrictModel):
    access_mode: Literal["days", "no_fixed_expiry"]
    access_days: int | None
    online: bool
    pdf: bool
    zip: bool
    update_policy: Literal["current_version"]

    @model_validator(mode="after")
    def explicit_duration(self):
        if self.access_mode == "days" and (self.access_days is None or not 1 <= self.access_days <= 3650):
            raise ValueError("Learning access must be 1–3650 days.")
        if self.access_mode == "no_fixed_expiry" and self.access_days is not None:
            raise ValueError("No fixed expiry requires null access_days.")
        return self


class SalesChannel(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000)

    @field_validator("url", mode="before")
    @classmethod
    def https_only(cls, value):
        if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value:
            raise ValueError("Channel URL must be HTTPS.")
        try:
            parts = urlsplit(value)
            if parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None:
                raise ValueError
            _ = parts.port
        except ValueError:
            raise ValueError("Channel URL must be HTTPS without credentials.") from None
        return value


class ProductInput(StrictModel):
    title: str = Field(min_length=1, max_length=300)
    category_id: int = Field(gt=0)
    synopsis: str = Field(default="", max_length=4000)
    audience: str = Field(default="", max_length=2000)
    prerequisites: str = Field(default="", max_length=2000)
    outcomes: list[str] = Field(default_factory=list, max_length=100)
    course_id: str | None = Field(default=None, min_length=1, max_length=300)
    ai_disclosure: str = Field(default="", max_length=4000)
    support_text: str = Field(default="", max_length=4000)
    channels: list[SalesChannel] = Field(default_factory=list, max_length=20)
    policy: AccessPolicy | None = None

    @field_validator("outcomes")
    @classmethod
    def normalize_outcomes(cls, values):
        if any(not value.strip() or len(value) > 2000 for value in values):
            raise ValueError("Outcomes must contain nonempty text.")
        return [value.strip() for value in values]


class ProductRecord(StrictModel):
    id: int
    revision: int
    status: str
    data: ProductInput


class IssuedPolicy(StrictModel):
    product_id: int
    course_id: str
    course_slug: str
    version: str
    package_hash: str
    access: AccessPolicy
    title: str
    support_text: str


class SalesChecklist(StrictModel):
    quality: bool
    sources: bool
    ai: bool
    mobile: bool
    downloads: bool


class _CategoryInput(StrictModel):
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=100)
    name: str = Field(min_length=1, max_length=200)
    sort_order: int
    enabled: bool


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _promise_hash(data: ProductInput) -> str:
    return hashlib.sha256(_json(data.model_dump()).encode("utf-8")).hexdigest()


def _page_offset(page: int) -> int:
    # Bound before multiplication or binding, for both management lists.
    if type(page) is not int or not 1 <= page <= ((2**63 - 1) // 20 + 1):
        raise BusinessError("invalid_filter", "Invalid page.", 400)
    return (page - 1) * 20


def _record(row) -> ProductRecord:
    try:
        data = ProductInput.model_validate_json(row["description"])
    except ValidationError:
        raise BusinessError("product_invalid", "Product data needs repair before sale.", 409) from None
    stored = (row["title"], row["category_id"], row["course_id"], row["access_days"], row["update_policy"])
    promised = (data.title, data.category_id, data.course_id,
                data.policy.access_days if data.policy else None, data.policy.update_policy if data.policy else None)
    if stored != promised:
        raise BusinessError("product_invalid", "Product storage and sales promises do not match.", 409)
    return ProductRecord(id=row["id"], revision=row["revision"], status=row["status"], data=data)


class ProductService:
    def __init__(self, db_path: Path, *, clock: Clock = utc_now):
        self.db_path = Path(db_path)
        self.clock = clock

    @staticmethod
    def _event(actor, action, object_id, *, error=None, changes=None, request_id=None):
        return AuditEvent(actor_admin_id=actor.admin_id if actor else None,
            object_type="category" if action.startswith("category.") else "product",
            object_id=str(object_id), action=action, reason=error.code if error else "completed",
            outcome="denied" if error else "success", request_id=actor.request_id if actor else request_id,
            changes={"error_code": error.code} if error else (changes or {}))

    def record_denial(self, actor: Actor | None, action: str, object_id: str,
                      error: BusinessError, *, request_id: str | None = None) -> None:
        if getattr(error, "denial_recorded", False):
            return
        if actor:
            request_id = actor.request_id
            with closing(open_readonly(self.db_path)) as connection:
                if not connection.execute("SELECT 1 FROM admins WHERE id=?", (actor.admin_id,)).fetchone():
                    actor = None
        record_denial(self.db_path, self._event(actor, action, object_id, error=error, request_id=request_id))
        error.denial_recorded = True

    @contextmanager
    def _write(self, actor, action, object_id):
        try:
            with transaction(self.db_path, immediate=True) as connection:
                if not connection.execute("SELECT 1 FROM admins WHERE id=? AND role='owner' AND enabled=1",
                                          (actor.admin_id,)).fetchone():
                    raise BusinessError("owner_required", "An enabled owner is required.", 403)
                yield connection
        except (BusinessError, ValidationError, sqlite3.IntegrityError) as exc:
            if isinstance(exc, ValidationError):
                error = BusinessError("invalid_product", "Invalid product or category fields.", 400)
            elif isinstance(exc, sqlite3.IntegrityError):
                error = BusinessError("product_conflict", "Category or course binding conflicts with existing data.", 409)
            else:
                error = exc
            self.record_denial(actor, action, str(object_id), error)
            raise error from None

    @staticmethod
    def _row(connection, product_id):
        row = connection.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if row is None:
            raise BusinessError("product_missing", "Product does not exist.", 404)
        return row

    @staticmethod
    def _revision(row, revision):
        if type(revision) is not int or revision != row["revision"]:
            raise BusinessError("stale_revision", "This form is stale; reload and try again.", 409)

    @staticmethod
    def _relations(connection, data):
        if not connection.execute("SELECT 1 FROM categories WHERE id=?", (data.category_id,)).fetchone():
            raise BusinessError("category_missing", "Choose an existing category.", 400)
        if data.course_id and not connection.execute("SELECT 1 FROM courses WHERE course_id=?", (data.course_id,)).fetchone():
            raise BusinessError("course_missing", "Choose an existing course.", 400)

    def _insert(self, connection, actor, data, *, request_id=None):
        self._relations(connection, data)
        now = to_db_time(self.clock())
        cursor = connection.execute("""INSERT INTO products
            (course_id, title, description, category_id, access_days, update_policy, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (data.course_id, data.title, _json(data.model_dump()), data.category_id,
             data.policy.access_days if data.policy else None, data.policy.update_policy if data.policy else None,
             actor.admin_id if actor else None, now, now))
        record = _record(self._row(connection, cursor.lastrowid))
        append_event(connection, self._event(actor, "product.create", record.id, request_id=request_id,
                                            changes={"status": "draft", "revision": 1}))
        return record

    def create(self, actor: Actor, data: ProductInput) -> ProductRecord:
        with self._write(actor, "product.create", "new") as connection:
            data = ProductInput.model_validate(data.model_dump())
            return self._insert(connection, actor, data)

    def update(self, actor: Actor, product_id: int, revision: int, data: ProductInput) -> ProductRecord:
        with self._write(actor, "product.update", product_id) as connection:
            row = self._row(connection, product_id)
            self._revision(row, revision)
            data = ProductInput.model_validate(data.model_dump())
            old = _record(row)
            if data.course_id != old.data.course_id:
                history = any(connection.execute(f"SELECT 1 FROM {table} WHERE product_id=? LIMIT 1",
                    (product_id,)).fetchone() for table in ("access_codes", "entitlements", "code_batches", "orders"))
                if history or row["status"] != "draft":
                    raise BusinessError("course_binding_locked", "The existing course binding must be preserved.", 409)
            self._relations(connection, data)
            changed = _promise_hash(old.data) != _promise_hash(data)
            connection.execute("""UPDATE products SET course_id=?, title=?, description=?, category_id=?,
                access_days=?, update_policy=?, revision=revision+1, updated_at=?,
                sales_check_json=CASE WHEN ? THEN NULL ELSE sales_check_json END,
                sales_checked_at=CASE WHEN ? THEN NULL ELSE sales_checked_at END,
                sales_package_hash=CASE WHEN ? THEN NULL ELSE sales_package_hash END
                WHERE id=? AND revision=?""",
                (data.course_id, data.title, _json(data.model_dump()), data.category_id,
                 data.policy.access_days if data.policy else None, data.policy.update_policy if data.policy else None,
                 to_db_time(self.clock()), changed, changed, changed, product_id, revision))
            updated = _record(self._row(connection, product_id))
            changes = {"status": updated.status, "revision": updated.revision}
            if data.policy:
                changes.update(access_days=data.policy.access_days, update_policy=data.policy.update_policy)
            append_event(connection, self._event(actor, "product.update", product_id, changes=changes))
            return updated

    def _inspect_sale(self, connection, record) -> tuple[sqlite3.Row, PackageInspection]:
        data = record.data
        if (not data.policy or not data.course_id or not all((data.synopsis, data.audience,
                data.prerequisites, data.outcomes, data.ai_disclosure, data.support_text, data.channels))):
            raise BusinessError("sale_fields_missing", "Complete the sales copy, course and explicit learning policy.", 409)
        if not any((data.policy.online, data.policy.pdf, data.policy.zip)):
            raise BusinessError("sale_formats_missing", "Choose at least one delivery format.", 409)
        category = connection.execute("SELECT enabled FROM categories WHERE id=?", (data.category_id,)).fetchone()
        if not category or not category["enabled"]:
            raise BusinessError("category_disabled", "The product category is disabled.", 409)
        course = connection.execute("SELECT * FROM courses WHERE course_id=?", (data.course_id,)).fetchone()
        if not course or course["status"] != "published":
            raise BusinessError("course_unpublished", "Publish the bound course before sale.", 409)
        root = Path(course["content_path"])
        inspection = inspect_package(root)
        manifest = CourseManifest.model_validate_json(read_verified_file(root, "manifest.json", inspection.fingerprint))
        if manifest.status != "published":
            raise BusinessError("course_unpublished", "Publish the course package before sale.", 409)
        if (inspection.course_id, inspection.slug, inspection.version) != (course["course_id"], course["slug"], course["version"]):
            raise BusinessError("package_identity", "Course catalog and package identity do not match.", 409)
        if course["package_hash"] and course["package_hash"] != inspection.fingerprint:
            raise BusinessError("package_changed", "Course package changed; inspect and publish it again.", 409)
        if data.policy.pdf and not inspection.pdf_ready:
            raise BusinessError("pdf_unavailable", "The promised PDF download is unavailable.", 409)
        if data.policy.zip and not inspection.zip_ready:
            raise BusinessError("zip_unavailable", "The promised ZIP download is unavailable.", 409)
        return course, inspection

    @staticmethod
    def _approval(data, course, inspection, checks):
        return {"promise_hash": _promise_hash(data), "checks": checks.model_dump(),
                "course_id": course["course_id"], "slug": course["slug"], "version": course["version"],
                "content_path": course["content_path"], "package_hash": inspection.fingerprint}

    def activate(self, actor: Actor, product_id: int, revision: int, checks: SalesChecklist) -> ProductRecord:
        with self._write(actor, "product.sales_check", product_id) as connection:
            row = self._row(connection, product_id)
            self._revision(row, revision)
            if row["status"] == "archived":
                raise BusinessError("product_archived", "Archived products cannot be activated.", 409)
            checks = SalesChecklist.model_validate(checks.model_dump())
            if not all(checks.model_dump().values()):
                raise BusinessError("sales_check_incomplete", "Confirm every sales checklist item again.", 409)
            record = _record(row)
            course, inspection = self._inspect_sale(connection, record)
            connection.execute("""UPDATE products SET status='active', sales_check_json=?, sales_checked_at=?,
                sales_package_hash=?, revision=revision+1, updated_at=? WHERE id=? AND revision=?""",
                (_json(self._approval(record.data, course, inspection, checks)), to_db_time(self.clock()),
                 inspection.fingerprint, to_db_time(self.clock()), product_id, revision))
            active = _record(self._row(connection, product_id))
            append_event(connection, self._event(actor, "product.sales_check", product_id,
                                                changes={"check_ok": True, "revision": active.revision}))
            return active

    def set_status(self, actor: Actor, product_id: int, revision: int,
                   status: Literal["paused", "archived"]) -> ProductRecord:
        with self._write(actor, "product.update", product_id) as connection:
            row = self._row(connection, product_id)
            self._revision(row, revision)
            if status not in ("paused", "archived") or (status == "paused" and row["status"] != "active"):
                raise BusinessError("product_transition", "This product status transition is unavailable.", 409)
            connection.execute("UPDATE products SET status=?, revision=revision+1, updated_at=? WHERE id=? AND revision=?",
                               (status, to_db_time(self.clock()), product_id, revision))
            record = _record(self._row(connection, product_id))
            append_event(connection, self._event(actor, "product.update", product_id,
                                                changes={"status": status, "revision": record.revision}))
            return record

    def require_sale_ready_in_tx(self, connection: sqlite3.Connection, product_id: int) -> IssuedPolicy:
        row = self._row(connection, product_id)
        if row["status"] != "active":
            raise BusinessError("product_not_active", "Only active products can issue new access.", 409)
        record = _record(row)
        course, inspection = self._inspect_sale(connection, record)
        try:
            approval = json.loads(row["sales_check_json"] or "null")
            checks = SalesChecklist.model_validate(approval["checks"])
            valid = (all(checks.model_dump().values()) and row["sales_checked_at"]
                     and approval == self._approval(record.data, course, inspection, checks)
                     and row["sales_package_hash"] == inspection.fingerprint)
        except (ValueError, TypeError, KeyError):
            valid = False
        if not valid:
            raise BusinessError("sales_approval_stale", "Sales approval is missing or stale; confirm the checklist again.", 409)
        return IssuedPolicy(product_id=product_id, course_id=course["course_id"], course_slug=course["slug"],
            version=inspection.version, package_hash=inspection.fingerprint, access=record.data.policy,
            title=record.data.title, support_text=record.data.support_text)

    def get_product(self, product_id: int) -> ProductRecord:
        with closing(open_readonly(self.db_path)) as connection:
            return _record(self._row(connection, product_id))

    def sale_readiness_error(self, product_id: int) -> BusinessError | None:
        with closing(open_readonly(self.db_path)) as connection:
            try:
                row = self._row(connection, product_id)
                if row["status"] == "active":
                    self.require_sale_ready_in_tx(connection, product_id)
                else:
                    self._inspect_sale(connection, _record(row))
                    raise BusinessError("sales_confirmation_required", "Confirm all checklist items to activate the product.", 409)
            except BusinessError as error:
                return error
        return None

    def list_products(self, *, status: str | None, category_id: int | None, title: str,
                      page: int) -> tuple[list[ProductRecord], int]:
        offset = _page_offset(page)
        if status is not None and status not in ("draft", "active", "paused", "archived"):
            raise BusinessError("invalid_filter", "Invalid product filter or page.", 400)
        conditions, values = [], []
        for column, value in (("status", status), ("category_id", category_id)):
            if value is not None:
                conditions.append(f"{column}=?")
                values.append(value)
        if title:
            conditions.append("instr(lower(title), lower(?)) > 0")
            values.append(title)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with closing(open_readonly(self.db_path)) as connection:
            total = connection.execute("SELECT count(*) FROM products" + where, values).fetchone()[0]
            rows = connection.execute("SELECT * FROM products" + where + " ORDER BY id DESC LIMIT 20 OFFSET ?",
                                      (*values, offset)).fetchall()
            return [_record(row) for row in rows], total

    def list_categories(self) -> list[dict]:
        with closing(open_readonly(self.db_path)) as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM categories ORDER BY sort_order, id")]

    def list_category_page(self, *, page: int) -> tuple[list[dict], int]:
        offset = _page_offset(page)
        with closing(open_readonly(self.db_path)) as connection:
            total = connection.execute("SELECT count(*) FROM categories").fetchone()[0]
            rows = connection.execute("SELECT * FROM categories ORDER BY sort_order, id LIMIT 20 OFFSET ?", (offset,))
            return [dict(row) for row in rows], total

    def list_courses(self) -> list[dict]:
        with closing(open_readonly(self.db_path)) as connection:
            return [dict(row) for row in connection.execute("SELECT course_id, title FROM courses ORDER BY title, course_id")]

    def save_category(self, actor: Actor, category_id: int | None, revision: int | None,
                      slug: str, name: str, sort_order: int, enabled: bool) -> int:
        action = "category.create" if category_id is None else "category.update"
        with self._write(actor, action, category_id if category_id is not None else "new") as connection:
            data = _CategoryInput(slug=slug, name=name, sort_order=sort_order, enabled=enabled)
            if category_id is None:
                category_id = connection.execute("""INSERT INTO categories
                    (slug, name, sort_order, enabled, created_by) VALUES (?, ?, ?, ?, ?)""",
                    (data.slug, data.name, data.sort_order, data.enabled, actor.admin_id)).lastrowid
                changes = {"revision": 1}
            else:
                row = connection.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
                if not row:
                    raise BusinessError("category_missing", "Category does not exist.", 404)
                self._revision(row, revision)
                connection.execute("""UPDATE categories SET slug=?, name=?, sort_order=?, enabled=?, revision=revision+1
                    WHERE id=? AND revision=?""", (data.slug, data.name, data.sort_order, data.enabled, category_id, revision))
                changes = {"revision": revision + 1, "enabled": data.enabled, "sort_order": data.sort_order}
            append_event(connection, self._event(actor, action, category_id, changes=changes))
            return category_id

    def ensure_draft_in_tx(self, connection: sqlite3.Connection, course_id: str) -> ProductRecord:
        """Import only, inside the caller's transaction; never overwrite a product."""
        if not connection.in_transaction:
            raise BusinessError("import_transaction", "Import requires the caller's transaction.", 409)
        row = connection.execute("SELECT * FROM products WHERE course_id=?", (course_id,)).fetchone()
        if row:
            return _record(row)
        course = connection.execute("SELECT * FROM courses WHERE course_id=?", (course_id,)).fetchone()
        if not course:
            raise BusinessError("course_missing", "Import requires an existing course.", 409)
        request_id = secrets.token_hex(16)
        slug = course["category"] if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", course["category"]) else (
            "import-" + hashlib.sha256(course["category"].encode()).hexdigest()[:20])
        category = connection.execute("SELECT id FROM categories WHERE slug=?", (slug,)).fetchone()
        if category:
            category_id = category["id"]
        else:
            category_id = connection.execute("INSERT INTO categories (slug, name, created_by) VALUES (?, ?, NULL)",
                                             (slug, course["category"])).lastrowid
            append_event(connection, self._event(None, "category.create", category_id,
                                                request_id=request_id, changes={"revision": 1}))
        return self._insert(connection, None,
            ProductInput(title=course["title"], category_id=category_id, course_id=course_id), request_id=request_id)
