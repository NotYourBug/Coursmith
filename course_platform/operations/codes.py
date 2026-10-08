"""Owner-only, transaction-owned issuance with one-time plaintext receipts."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictInt, ValidationError, field_validator

from ..audit import AuditEvent, append_event, record_denial
from ..content_inspection import inspect_package
from ..database import open_readonly, to_db_time, transaction
from ..domain import Actor, BusinessError, Clock, utc_now
from .products import IssuedPolicy, ProductService, StrictModel, _page_offset


_CREDENTIAL = re.compile(r"(?:CS|LK)-[A-Za-z0-9_-]+")


class BatchInput(StrictModel):
    product_id: StrictInt = Field(gt=0)
    count: StrictInt = Field(default=1, ge=1, le=200)
    purpose: Literal['sale', 'test', 'gift']
    activation_days: StrictInt = Field(default=30, ge=1, le=365)
    note: str = Field(default="", max_length=1000)

    @field_validator("note")
    @classmethod
    def no_credentials(cls, value):
        if _CREDENTIAL.search(value):
            raise ValueError("Do not store credentials in notes.")
        return value


@dataclass(frozen=True)
class IssuedCode:
    public_id: str
    raw_code: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class BatchReceipt:
    batch_id: int
    codes: tuple[IssuedCode, ...]
    replayed: bool


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf8")).hexdigest()


class CodeService:
    def __init__(self, db_path: Path, *, clock: Clock = utc_now):
        self.db_path = Path(db_path)
        self.clock = clock
        self.products = ProductService(self.db_path, clock=clock)

    @staticmethod
    def _event(actor, action, object_id, *, kind="batch", error=None, changes=None, request_id=None):
        return AuditEvent(actor_admin_id=actor.admin_id if actor else None,
            object_type=kind, object_id=str(object_id), action=action,
            reason=error.code if error else "completed", outcome="denied" if error else "success",
            request_id=actor.request_id if actor else request_id,
            changes={"error_code": error.code} if error else (changes or {}))

    def record_denial(self, actor: Actor | None, action: str, object_id: str, error: BusinessError,
                      *, request_id: str | None = None, kind="batch") -> None:
        if getattr(error, "denial_recorded", False):
            return
        if not isinstance(object_id, str) or not re.fullmatch(r"(?:[0-9]{1,19}|new|invalid)", object_id):
            object_id = "invalid"
        if actor:
            request_id = actor.request_id
            with closing(open_readonly(self.db_path)) as connection:
                if not connection.execute("SELECT 1 FROM admins WHERE id=?", (actor.admin_id,)).fetchone():
                    actor = None
        record_denial(self.db_path, self._event(actor, action, object_id, kind=kind, error=error, request_id=request_id))
        # Only suppress the route denial after durable recording succeeds.
        error.denial_recorded = True

    @staticmethod
    def _owner(connection, actor):
        if not connection.execute("SELECT 1 FROM admins WHERE id=? AND role='owner' AND enabled=1", (actor.admin_id,)).fetchone():
            raise BusinessError("owner_required", "An enabled owner is required.", 403)

    @contextmanager
    def _write(self, actor, action, object_id, *, kind="batch"):
        try:
            with transaction(self.db_path, immediate=True) as connection:
                self._owner(connection, actor)
                yield connection
        except (BusinessError, ValidationError, sqlite3.IntegrityError) as exc:
            error = exc if isinstance(exc, BusinessError) else BusinessError(
                "invalid_batch" if isinstance(exc, ValidationError) else "code_conflict",
                "Invalid code fields." if isinstance(exc, ValidationError) else "The operation conflicts with existing data.",
                400 if isinstance(exc, ValidationError) else 409)
            self.record_denial(actor, action, str(object_id), error, kind=kind)
            raise error from None

    @staticmethod
    def _identity(value):
        if type(value) is not int or not 1 <= value <= 2**63 - 1:
            raise BusinessError("invalid_code", "Invalid code or batch ID.", 400)

    @staticmethod
    def _revision(row, revision):
        if type(revision) is not int or revision != row["revision"]:
            raise BusinessError("stale_revision", "This form is stale; reload and try again.", 409)

    @staticmethod
    def _reason(reason):
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000 or _CREDENTIAL.search(reason):
            raise BusinessError("invalid_reason", "Provide a reason of 1–1000 characters without credentials.", 400)
        return reason.strip()

    @staticmethod
    def _request(connection, actor, action, key, payload):
        if not isinstance(key, str) or not 1 <= len(key.strip()) <= 200 or _CREDENTIAL.search(key):
            raise BusinessError("idempotency_required", "Provide a valid idempotency key.", 400)
        key_hash, digest = _hash(key.strip()), _hash(_json(payload))
        row = connection.execute("""SELECT request_digest, object_id FROM operation_requests
            WHERE actor_admin_id=? AND action=? AND idempotency_key=?""", (actor.admin_id, action, key_hash)).fetchone()
        if row:
            if row["request_digest"] != digest:
                raise BusinessError("idempotency_conflict", "This key was used for a different request.", 409)
            return BatchReceipt(int(row["object_id"]), (), True), key_hash, digest
        return None, key_hash, digest

    def _save_request(self, connection, actor, action, key_hash, digest, receipt):
        connection.execute("""INSERT INTO operation_requests
            (actor_admin_id, action, idempotency_key, request_digest, object_type, object_id, created_at)
            VALUES (?, ?, ?, ?, 'batch', ?, ?)""",
            (actor.admin_id, action, key_hash, digest, str(receipt.batch_id), to_db_time(self.clock())))

    @staticmethod
    def _batch(connection, batch_id):
        CodeService._identity(batch_id)
        row = connection.execute("SELECT id, revision FROM code_batches WHERE id=?", (batch_id,)).fetchone()
        if not row:
            raise BusinessError("batch_missing", "Batch does not exist.", 404)
        return row

    @staticmethod
    def _code(connection, code_id):
        CodeService._identity(code_id)
        row = connection.execute("""SELECT c.id, c.course_id, c.product_id, c.batch_id, c.order_id, c.revision,
            c.used_at, c.voided_at, c.issued_policy_json, c.verified_at, c.verified_by, c.verified_reason,
            b.purpose, b.activation_days, b.notes
            FROM access_codes c LEFT JOIN code_batches b ON b.id=c.batch_id WHERE c.id=?""", (code_id,)).fetchone()
        if not row:
            raise BusinessError("code_missing", "Code does not exist.", 404)
        return row

    @staticmethod
    def _unused(row):
        if row["used_at"] or row["voided_at"]:
            raise BusinessError("code_not_unused", "Only unused, unrevoked codes can be changed.", 409)

    def _fulfillable(self, connection, policy):
        # Current sale approval gates *new* codes; original promises govern
        # fulfillment. Changes to title/duration never replace the snapshot.
        self.products.require_sale_ready_in_tx(connection, policy.product_id)
        course = connection.execute("SELECT slug, version, content_path FROM courses WHERE course_id=?", (policy.course_id,)).fetchone()
        if not course:
            raise BusinessError("issued_content_unavailable", "Resolve the original course and sales check before issuing replacements.", 409)
        package = inspect_package(Path(course["content_path"]))
        if ((package.course_id, package.slug, package.version, package.fingerprint) !=
            (policy.course_id, policy.course_slug, policy.version, policy.package_hash)
            or (course["slug"], course["version"]) != (policy.course_slug, policy.version)
            or (policy.access.pdf and not package.pdf_ready) or (policy.access.zip and not package.zip_ready)):
            raise BusinessError("issued_content_unavailable", "Resolve the original course and sales check before issuing replacements.", 409)

    @staticmethod
    def _policy(row):
        try:
            policy = IssuedPolicy.model_validate_json(row["issued_policy_json"] or "null")
        except ValidationError:
            raise BusinessError("snapshot_unverified", "The original issued policy must be verified before replacement.", 409) from None
        if policy.product_id != row["product_id"] or ("course_id" in row.keys() and policy.course_id != row["course_id"]):
            raise BusinessError("snapshot_unverified", "The original policy does not match its code.", 409)
        return policy

    def _insert(self, connection, actor, policies, data, *, order_ids, replaces, storage_key):
        policy = policies[0]
        now = self.clock()
        expires = now + timedelta(days=data.activation_days)
        batch_id = connection.execute("""INSERT INTO code_batches
            (product_id, quantity, purpose, idempotency_key, access_days, activation_days, update_policy,
             course_version, package_hash, notes, created_by, created_at, issued_policy_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (data.product_id, len(policies), data.purpose, storage_key, policy.access.access_days, data.activation_days,
             policy.access.update_policy, policy.version, policy.package_hash, data.note, actor.admin_id,
             to_db_time(now), _json(policy.model_dump()))).lastrowid
        codes = []
        for item, order_id, replaces_id in zip(policies, order_ids, replaces, strict=True):
            raw = "CS-" + secrets.token_urlsafe(24)
            public_id = "CODE-" + secrets.token_hex(12)
            connection.execute("""INSERT INTO access_codes
                (course_id, code_hash, created_at, expires_at, batch_id, product_id, order_id, code_number,
                 access_days, update_policy, course_version, package_hash, purpose, replaces_code_id, created_by, issued_policy_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (item.course_id, _hash(raw), to_db_time(now), to_db_time(expires), batch_id, data.product_id, order_id,
                 public_id, item.access.access_days, item.access.update_policy, item.version, item.package_hash,
                 data.purpose, replaces_id, actor.admin_id, _json(item.model_dump())))
            codes.append(IssuedCode(public_id, raw, expires))
        return BatchReceipt(batch_id, tuple(codes), False)

    def issue_batch(self, actor: Actor, data: BatchInput, idempotency_key: str) -> BatchReceipt:
        with self._write(actor, "code.issue", "new") as connection:
            data = BatchInput.model_validate(data.model_dump())
            # Replays precede sale checks, including products since paused.
            replay, _, _ = self._request(connection, actor, "code.issue", idempotency_key, {"data": data.model_dump(), "order_id": None})
            if replay:
                return replay
            policy = self.products.require_sale_ready_in_tx(connection, data.product_id)
            return self.issue_in_tx(connection, actor, policy, data, order_id=None, idempotency_key=idempotency_key)

    def issue_in_tx(self, connection: sqlite3.Connection, actor: Actor, policy: IssuedPolicy, data: BatchInput,
                    *, order_id: int | None, idempotency_key: str) -> BatchReceipt:
        """Caller owns BEGIN IMMEDIATE, commit/rollback and post-rollback denials.

        Task9 passes the order's recorded snapshot. This method never replaces
        it with current product promises or commits the caller's transaction.
        """
        if not connection.in_transaction:
            raise BusinessError("code_transaction", "Issuance requires an active writer transaction.", 409)
        self._owner(connection, actor)
        data = BatchInput.model_validate(data.model_dump())
        replay, key_hash, digest = self._request(connection, actor, "code.issue", idempotency_key,
                                                {"data": data.model_dump(), "order_id": order_id})
        if replay:
            return replay
        policy = IssuedPolicy.model_validate(policy.model_dump())
        if policy.product_id != data.product_id or _CREDENTIAL.search(_json(policy.model_dump())):
            raise BusinessError("snapshot_unverified", "The issued policy does not match the product or contains credentials.", 409)
        self._fulfillable(connection, policy)
        if order_id is not None:
            self._identity(order_id)
            order = connection.execute("SELECT product_id, status FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order or order["product_id"] != data.product_id or order["status"] == "refunded":
                raise BusinessError("order_unavailable", "Choose a paid order for this product.", 409)
        receipt = self._insert(connection, actor, [policy] * data.count, data,
            order_ids=[order_id] * data.count, replaces=[None] * data.count, storage_key=_hash("code.issue:" + key_hash))
        self._save_request(connection, actor, "code.issue", key_hash, digest, receipt)
        append_event(connection, self._event(actor, "code.issue", receipt.batch_id, changes={"quantity": data.count,
            "access_days": policy.access.access_days, "activation_days": data.activation_days, "update_policy": "current_version"}))
        return receipt

    def _void(self, connection, row, reason):
        connection.execute("UPDATE access_codes SET voided_at=?, void_reason=?, revision=revision+1 WHERE id=? AND used_at IS NULL AND voided_at IS NULL",
                           (to_db_time(self.clock()), reason, row["id"]))

    def revoke(self, actor: Actor, code_id: int, revision: int, reason: str) -> None:
        with self._write(actor, "code.void", code_id, kind="code") as connection:
            row = self._code(connection, code_id)
            self._revision(row, revision)
            self._unused(row)
            reason = self._reason(reason)
            self._void(connection, row, reason)
            if row["batch_id"]:
                connection.execute("UPDATE code_batches SET revision=revision+1 WHERE id=?", (row["batch_id"],))
            append_event(connection, self._event(actor, "code.void", code_id, kind="code", changes={"revision": revision + 1}))

    def revoke_unused(self, actor: Actor, batch_id: int, revision: int, reason: str) -> int:
        with self._write(actor, "code.void", batch_id) as connection:
            batch = self._batch(connection, batch_id)
            self._revision(batch, revision)
            reason = self._reason(reason)
            affected = connection.execute("""UPDATE access_codes SET voided_at=?, void_reason=?, revision=revision+1
                WHERE batch_id=? AND used_at IS NULL AND voided_at IS NULL""", (to_db_time(self.clock()), reason, batch_id)).rowcount
            if not affected:
                raise BusinessError("no_unused_codes", "This batch has no unused codes to revoke.", 409)
            connection.execute("UPDATE code_batches SET revision=revision+1 WHERE id=?", (batch_id,))
            append_event(connection, self._event(actor, "code.void", batch_id, changes={"revision": revision + 1}))
            return affected

    def _replace(self, connection, actor, rows, reason, key_hash, digest):
        policies = [self._policy(row) for row in rows]
        # One batch has one full policy; never silently merge divergent promises.
        if any(policy != policies[0] for policy in policies[1:]):
            raise BusinessError("snapshot_unverified", "This batch has inconsistent issued policies; verify each code first.", 409)
        self._fulfillable(connection, policies[0])
        data = BatchInput(product_id=policies[0].product_id, count=len(rows), purpose=rows[0]["purpose"],
                          activation_days=rows[0]["activation_days"], note=rows[0]["notes"])
        for row in rows:
            self._unused(row)
            if row["order_id"] is not None:
                order = connection.execute("SELECT status FROM orders WHERE id=?", (row["order_id"],)).fetchone()
                if not order or order["status"] == "refunded":
                    raise BusinessError("order_unavailable", "Refunded orders cannot receive replacements.", 409)
            self._void(connection, row, reason)
        receipt = self._insert(connection, actor, policies, data, order_ids=[row["order_id"] for row in rows],
            replaces=[row["id"] for row in rows], storage_key=_hash("code.reissue:" + key_hash))
        for row, code in zip(rows, receipt.codes, strict=True):
            # A replacement retains the original recorded purchase proof;
            # resetting a credential must not invent or overwrite that evidence.
            if row["order_id"] is not None:
                connection.execute("""UPDATE access_codes SET verified_at=?, verified_by=?, verified_reason=?
                    WHERE code_number=?""", (row["verified_at"], row["verified_by"], row["verified_reason"], code.public_id))
        for batch_id in {row["batch_id"] for row in rows}:
            connection.execute("UPDATE code_batches SET revision=revision+1 WHERE id=?", (batch_id,))
        self._save_request(connection, actor, "code.reissue", key_hash, digest, receipt)
        return receipt

    def replace(self, actor: Actor, code_id: int, revision: int, reason: str, idempotency_key: str) -> BatchReceipt:
        with self._write(actor, "code.reissue", code_id, kind="code") as connection:
            self._identity(code_id)
            reason = self._reason(reason)
            replay, key_hash, digest = self._request(connection, actor, "code.reissue", idempotency_key,
                {"code_id": code_id, "revision": revision, "reason": reason})
            if replay:
                return replay
            row = self._code(connection, code_id)
            self._revision(row, revision)
            self._unused(row)
            receipt = self._replace(connection, actor, [row], reason, key_hash, digest)
            append_event(connection, self._event(actor, "code.reissue", code_id, kind="code", changes={"quantity": 1, "revision": revision + 1}))
            return receipt

    def replace_unused(self, actor: Actor, batch_id: int, revision: int, reason: str, idempotency_key: str) -> BatchReceipt:
        with self._write(actor, "code.reissue", batch_id) as connection:
            self._identity(batch_id)
            reason = self._reason(reason)
            replay, key_hash, digest = self._request(connection, actor, "code.reissue", idempotency_key,
                {"batch_id": batch_id, "revision": revision, "reason": reason})
            if replay:
                return replay
            batch = self._batch(connection, batch_id)
            self._revision(batch, revision)
            ids = connection.execute("SELECT id FROM access_codes WHERE batch_id=? AND used_at IS NULL AND voided_at IS NULL ORDER BY id", (batch_id,)).fetchall()
            if not ids:
                raise BusinessError("no_unused_codes", "This batch has no unused codes to replace.", 409)
            rows = [self._code(connection, row["id"]) for row in ids]
            receipt = self._replace(connection, actor, rows, reason, key_hash, digest)
            append_event(connection, self._event(actor, "code.reissue", batch_id, changes={"quantity": len(rows), "revision": revision + 1}))
            return receipt

    def list_batches(self, *, page: int = 1):
        offset = _page_offset(page)
        with closing(open_readonly(self.db_path)) as connection:
            total = connection.execute("SELECT count(*) FROM code_batches").fetchone()[0]
            batches = connection.execute("""WITH page AS (
                SELECT b.id, b.product_id, b.quantity, b.purpose, b.created_at, b.created_by,
                    p.title AS product_title, a.username AS creator
                FROM code_batches b JOIN products p ON p.id=b.product_id
                LEFT JOIN admins a ON a.id=b.created_by
                ORDER BY b.id DESC LIMIT 20 OFFSET ?), states AS (
                SELECT c.batch_id, CASE WHEN c.used_at IS NOT NULL THEN 'redeemed'
                    WHEN c.voided_at IS NOT NULL THEN 'voided'
                    WHEN c.expires_at IS NOT NULL AND c.expires_at<=? THEN 'expired'
                    ELSE 'unused' END AS state FROM access_codes c JOIN page ON page.id=c.batch_id)
                SELECT page.*, count(CASE WHEN states.state='unused' THEN 1 END) AS unused,
                    count(CASE WHEN states.state='redeemed' THEN 1 END) AS redeemed,
                    count(CASE WHEN states.state='voided' THEN 1 END) AS voided,
                    count(CASE WHEN states.state='expired' THEN 1 END) AS expired
                FROM page LEFT JOIN states ON states.batch_id=page.id GROUP BY page.id ORDER BY page.id DESC""",
                (offset, to_db_time(self.clock()))).fetchall()
            result = [dict(row) for row in batches]
            for batch in result:
                for key, fallback in (("product_title", f"商品 {batch['product_id']}"),
                                      ("creator", "未记录")):
                    value = batch[key]
                    if not value or _CREDENTIAL.search(value) or re.search(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43,}(?![A-Za-z0-9_-])", value):
                        batch[key] = fallback
                del batch["created_by"]
            return result, total

    def list_issue_products(self):
        with closing(open_readonly(self.db_path)) as connection:
            return [dict(row) for row in connection.execute("SELECT id, title FROM products WHERE status='active' ORDER BY id")]

    def get_batch(self, batch_id: int, *, page: int = 1):
        self._identity(batch_id)
        offset = _page_offset(page)
        with closing(open_readonly(self.db_path)) as connection:
            batch = connection.execute("""SELECT id, product_id, quantity, purpose, activation_days,
                revision, created_at, notes FROM code_batches WHERE id=?""", (batch_id,)).fetchone()
            if not batch:
                raise BusinessError("batch_missing", "Batch does not exist.", 404)
            codes = connection.execute("""SELECT c.id, c.code_number, c.revision, c.expires_at, c.used_at, c.voided_at,
                c.replaces_code_id, e.id AS entitlement_id FROM access_codes c
                LEFT JOIN entitlements e ON e.source_code_id=c.id
                WHERE c.batch_id=? ORDER BY c.id LIMIT 20 OFFSET ?""", (batch_id, offset)).fetchall()
            total = connection.execute("SELECT count(*) FROM access_codes WHERE batch_id=?", (batch_id,)).fetchone()[0]
            return dict(batch), [dict(row) for row in codes], total
