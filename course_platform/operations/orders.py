"""Owner-recorded shop payments and atomic delivery/refund orchestration."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from pydantic import Field, StrictInt, ValidationError, field_validator

from ..audit import AuditEvent, append_event
from ..database import from_db_time, open_readonly, to_db_time, transaction
from ..delivery.entitlements import EntitlementService
from ..domain import Actor, BusinessError, Clock, utc_now
from .codes import BatchInput, BatchReceipt, CodeService, IssuedCode, _hash, _json
from .products import IssuedPolicy, StrictModel, _page_offset


class OrderInput(StrictModel):
    channel: str = Field(min_length=1, max_length=200)
    shop_id: str = Field(min_length=1, max_length=200)
    external_order_id: str = Field(min_length=1, max_length=300, repr=False)
    product_id: int = Field(gt=0)
    paid_cents: StrictInt = Field(ge=0, le=2**63 - 1)
    paid_at: datetime
    note: str = Field(max_length=1000, repr=False)

    @field_validator("channel", "shop_id", "external_order_id", "note")
    @classmethod
    def safe_text(cls, value):
        if re.search(r"(?:CS|LK)-[A-Za-z0-9_-]+|(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])", value):
            raise ValueError("Do not store credentials in order fields.")
        if any(ord(c) < 32 and c not in "\n\t" for c in value):
            raise ValueError("Invalid order text.")
        return value

    @field_validator("paid_at")
    @classmethod
    def utc_payment(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Payment time requires an explicit timezone.")
        return value.astimezone(timezone.utc)


class OrderRecord(StrictModel):
    id: int
    revision: int
    status: str
    data: OrderInput = Field(repr=False)


def display_time(value: str | datetime | None) -> str:
    if value is None:
        return "—"
    instant = from_db_time(value) if isinstance(value, str) else value
    return instant.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S Asia/Shanghai")


def require_delivery_origin(site_origin: str) -> str:
    try:
        parts = urlsplit(site_origin)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.path not in ("", "/") or parts.query or parts.fragment
            or "\\" in site_origin or any(ord(c) <= 32 for c in site_origin)):
            raise ValueError
        _ = parts.port
    except ValueError:
        raise BusinessError("delivery_origin", "Configure a canonical HTTPS course site before issuing delivery copy.", 409) from None
    return site_origin.rstrip('/')


def delivery_text(policy: IssuedPolicy, code: IssuedCode, site_origin: str) -> str:
    origin = require_delivery_origin(site_origin)
    formats = "、".join(name for enabled, name in ((policy.access.online, "在线"),
        (policy.access.pdf, "PDF"), (policy.access.zip, "ZIP")) if enabled)
    duration = f"激活后{policy.access.access_days}天，以激活结果显示的学习截止时间为准" if policy.access.access_days is not None else "无固定到期日"
    return (f"商品：{policy.title}\n课程站入口：{origin}/courses/{quote(policy.course_slug, safe='')}\n"
        f"激活码：{code.raw_code}\n激活截止：{display_time(code.expires_at)}\n"
        f"学习期限：{duration}（与激活截止不同）\n交付格式：{formats}\n"
        f"内容版本：{policy.version}（仅当前版本）\n售后：{policy.support_text}")


_ORDER_COLUMNS = "id, product_id, channel, shop, external_order_id, amount_cents, status, notes, paid_at, revision, delivery_state"


def _record(row):
    try:
        return OrderRecord(id=row["id"], revision=row["revision"],
            status="refunded" if row["status"] == "refunded" else row["delivery_state"],
            data=OrderInput(channel=row["channel"], shop_id=row["shop"], external_order_id=row["external_order_id"],
                product_id=row["product_id"], paid_cents=row["amount_cents"], paid_at=from_db_time(row["paid_at"]), note=row["notes"]))
    except (ValidationError, ValueError, TypeError):
        raise BusinessError("order_unverified", "Verify the original payment time and safe order record before continuing.", 409) from None


class OrderService:
    def __init__(self, db_path: Path, *, clock: Clock = utc_now):
        self.db_path = Path(db_path)
        self.clock = clock
        self.codes = CodeService(self.db_path, clock=clock)
        self.entitlements = EntitlementService(self.db_path, clock=clock)

    @staticmethod
    def _event(actor, action, identity, *, request_id, error=None, changes=None):
        identity = str(identity) if str(identity) in ("new", "invalid") or re.fullmatch(r"[0-9]{1,19}", str(identity)) else "invalid"
        return AuditEvent(actor_admin_id=actor.admin_id if actor else None, object_type="order", object_id=identity,
            action=action, reason=error.code if error else "completed", outcome="denied" if error else "success",
            request_id=_hash(request_id if isinstance(request_id, str) else "invalid"),
            changes={"error_code": error.code} if error else (changes or {}))

    def record_denial(self, actor, action, identity, error, *, request_id):
        if getattr(error, "denial_recorded", False):
            return
        if action not in ("order.create", "order.issue", "order.deliver", "order.attach", "order.refund", "order.query"):
            raise ValueError("Unsupported order audit action.")
        try:
            with transaction(self.db_path, immediate=True) as connection:
                if actor and not connection.execute("SELECT 1 FROM admins WHERE id=?", (actor.admin_id,)).fetchone():
                    actor = None
                event = self._event(actor, action, identity, request_id=request_id, error=error)
                if not connection.execute("""SELECT 1 FROM admin_events WHERE action=? AND object_type='order'
                    AND object_id=? AND request_id=? AND outcome='denied'""",
                    (action, event.object_id, event.request_id)).fetchone():
                    append_event(connection, event)
            error.denial_recorded = True
        except sqlite3.DatabaseError:
            raise BusinessError("audit_unavailable", "Unable to record the operation outcome.", 503) from None

    @contextmanager
    def _write(self, actor, action, identity):
        try:
            with transaction(self.db_path, immediate=True) as connection:
                CodeService._owner(connection, actor)
                yield connection
        except (BusinessError, ValidationError, sqlite3.DatabaseError, ValueError, TypeError, OverflowError) as exc:
            error = exc if isinstance(exc, BusinessError) else BusinessError(
                "invalid_order" if isinstance(exc, ValidationError) else "order_conflict",
                "Invalid order fields." if isinstance(exc, ValidationError) else "The order operation conflicts with existing records.",
                400 if isinstance(exc, ValidationError) else 409)
            self.record_denial(actor, action, identity, error, request_id=actor.request_id)
            raise error from None

    @staticmethod
    def _row(connection, identity):
        CodeService._identity(identity)
        row = connection.execute(f"SELECT {_ORDER_COLUMNS} FROM orders WHERE id=?", (identity,)).fetchone()
        if not row:
            raise BusinessError("order_missing", "Order does not exist.", 404)
        return row

    @staticmethod
    def _paid(row):
        if row["status"] != "paid" or not row["paid_at"]:
            raise BusinessError("order_unavailable", "A verified paid order is required.", 409)

    def _save(self, connection, actor, action, key_hash, digest, identity):
        connection.execute("""INSERT INTO operation_requests
            (actor_admin_id, action, idempotency_key, request_digest, object_type, object_id, created_at)
            VALUES (?, ?, ?, ?, 'order', ?, ?)""", (actor.admin_id, action, key_hash, digest, str(identity), to_db_time(self.clock())))

    @staticmethod
    def _policy(connection, identity):
        row = connection.execute("SELECT product_id, issued_policy_json FROM orders WHERE id=?", (identity,)).fetchone()
        return CodeService._policy(row)

    def record(self, actor: Actor, data: OrderInput, idempotency_key: str) -> OrderRecord:
        with self._write(actor, "order.create", "new") as connection:
            data = OrderInput.model_validate(data.model_dump())
            replay, key, digest = CodeService._request(connection, actor, "order.create", idempotency_key, data.model_dump(mode="json"))
            if replay:
                return _record(self._row(connection, replay.batch_id))
            if data.paid_at > self.clock():
                raise BusinessError("invalid_payment_time", "Payment must already have occurred.", 400)
            policy = self.codes.products.require_sale_ready_in_tx(connection, data.product_id)
            identity = connection.execute("""INSERT INTO orders
                (product_id, channel, shop, external_order_id, amount_cents, notes, created_by, created_at,
                 paid_at, delivery_state, issued_policy_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'recorded', ?)""",
                (data.product_id, data.channel, data.shop_id, data.external_order_id, data.paid_cents, data.note,
                 actor.admin_id, to_db_time(self.clock()), to_db_time(data.paid_at), _json(policy.model_dump()))).lastrowid
            self._save(connection, actor, "order.create", key, digest, identity)
            append_event(connection, self._event(actor, "order.create", identity, request_id=actor.request_id,
                changes={"amount_cents": data.paid_cents, "status": "paid", "revision": 1}))
            return _record(self._row(connection, identity))

    def issue(self, actor: Actor, order_id: int, revision: int, idempotency_key: str) -> BatchReceipt:
        with self._write(actor, "order.issue", order_id) as connection:
            CodeService._identity(order_id)
            replay, key, digest = CodeService._request(connection, actor, "order.issue", idempotency_key,
                {"order_id": order_id, "revision": revision})
            if replay:
                return replay
            row = self._row(connection, order_id)
            CodeService._revision(row, revision)
            self._paid(row)
            if (row["delivery_state"] != "recorded"
                or connection.execute("SELECT 1 FROM access_codes WHERE order_id=?", (order_id,)).fetchone()
                or connection.execute("SELECT 1 FROM entitlements WHERE order_id=?", (order_id,)).fetchone()):
                raise BusinessError("order_already_issued", "This order already has an authorization; use the existing code support flow.", 409)
            policy = self._policy(connection, order_id)
            receipt = self.codes.issue_in_tx(connection, actor, policy,
                BatchInput(product_id=row["product_id"], count=1, purpose="sale", activation_days=30),
                order_id=order_id, idempotency_key="order-issue:" + key)
            # Provenance is a real owner verification of the bound recorded payment,
            # not caller-supplied reset prose. Redemption copies this evidence.
            connection.execute("""UPDATE access_codes SET verified_at=?, verified_by=?, verified_reason=?
                WHERE batch_id=? AND order_id=?""", (to_db_time(self.clock()), actor.admin_id,
                "经营者核验店铺已付款订单并登记发行", receipt.batch_id, order_id))
            connection.execute("UPDATE orders SET delivery_state='code_ready', revision=revision+1 WHERE id=?", (order_id,))
            # For issuance the completed object is the batch, with no cached raw receipt.
            connection.execute("""INSERT INTO operation_requests
                (actor_admin_id, action, idempotency_key, request_digest, object_type, object_id, created_at)
                VALUES (?, 'order.issue', ?, ?, 'batch', ?, ?)""", (actor.admin_id, key, digest, str(receipt.batch_id), to_db_time(self.clock())))
            append_event(connection, self._event(actor, "order.issue", order_id, request_id=actor.request_id, changes={"revision": revision + 1}))
            return receipt

    def confirm_delivery(self, actor: Actor, order_id: int, revision: int, idempotency_key: str) -> OrderRecord:
        with self._write(actor, "order.deliver", order_id) as connection:
            CodeService._identity(order_id)
            replay, key, digest = CodeService._request(connection, actor, "order.deliver", idempotency_key,
                {"order_id": order_id, "revision": revision})
            if replay:
                return _record(self._row(connection, order_id))
            row = self._row(connection, order_id)
            CodeService._revision(row, revision)
            self._paid(row)
            if row["delivery_state"] not in ("code_ready", "activated"):
                raise BusinessError("delivery_unavailable", "Issue or attach the order code before confirming delivery.", 409)
            connection.execute("""UPDATE orders SET delivered_at=coalesce(delivered_at, ?),
                delivery_state=CASE WHEN delivery_state='activated' THEN 'activated' ELSE 'delivered' END,
                revision=revision+1 WHERE id=?""", (to_db_time(self.clock()), order_id))
            self._save(connection, actor, "order.deliver", key, digest, order_id)
            append_event(connection, self._event(actor, "order.deliver", order_id, request_id=actor.request_id, changes={"revision": revision + 1}))
            return _record(self._row(connection, order_id))

    @staticmethod
    def _chain(connection, *, order_id=None, code_id=None):
        # Traverse both directions; UNION terminates even corrupted cycles.
        return connection.execute("""WITH RECURSIVE chain(id) AS (
            SELECT id FROM access_codes WHERE order_id=? OR id=?
            UNION SELECT c.id FROM access_codes c JOIN chain x ON c.replaces_code_id=x.id OR c.id=(
                SELECT replaces_code_id FROM access_codes WHERE id=x.id))
            SELECT c.id, c.batch_id, c.order_id, c.product_id, c.course_id, c.used_at, c.voided_at,
                c.issued_policy_json, c.purpose, c.created_by, c.verified_at, c.verified_by, c.verified_reason,
                b.purpose AS batch_purpose, b.created_by AS batch_creator, b.issued_policy_json AS batch_policy
            FROM access_codes c JOIN chain x ON x.id=c.id LEFT JOIN code_batches b ON b.id=c.batch_id""",
            (order_id, code_id)).fetchall()

    def attach_code(self, actor: Actor, order_id: int, revision: int, public_code_id: str,
                    verification_reason: str, idempotency_key: str) -> OrderRecord:
        with self._write(actor, "order.attach", order_id) as connection:
            CodeService._identity(order_id)
            reason = self.entitlements._reason(verification_reason)
            if not isinstance(public_code_id, str) or not re.fullmatch(r"CODE-[0-9a-f]{24}", public_code_id):
                raise BusinessError("invalid_code", "Enter the public code number, never the activation code.", 400)
            replay, key, digest = CodeService._request(connection, actor, "order.attach", idempotency_key,
                {"order_id": order_id, "revision": revision, "public_code_id": public_code_id, "reason": reason})
            if replay:
                return _record(self._row(connection, order_id))
            row = self._row(connection, order_id)
            CodeService._revision(row, revision)
            self._paid(row)
            policy = self._policy(connection, order_id)
            code = connection.execute("SELECT id, used_at, voided_at FROM access_codes WHERE code_number=?", (public_code_id,)).fetchone()
            if not code or code["voided_at"]:
                raise BusinessError("code_unavailable", "Choose a verifiable original code.", 409)
            chain = self._chain(connection, code_id=code["id"])
            ids = [item["id"] for item in chain]
            live = [item for item in chain if not item["voided_at"]]
            if len(live) != 1 or live[0]["id"] != code["id"]:
                raise BusinessError("association_conflict", "An order must have exactly one original usable authorization chain.", 409)
            if any(item["order_id"] not in (None, order_id) or item["purpose"] != "sale"
                or item["batch_purpose"] != "sale" or not item["created_by"] or item["created_by"] != item["batch_creator"]
                or CodeService._policy(item) != policy
                or IssuedPolicy.model_validate_json(item["batch_policy"] or "null") != policy for item in chain):
                raise BusinessError("association_conflict", "Verify the code's original product, course, policy and sale provenance.", 409)
            placeholders = ",".join("?" for _ in ids)
            if (connection.execute(f"SELECT 1 FROM access_codes WHERE order_id=? AND id NOT IN ({placeholders})", (order_id, *ids)).fetchone()
                or connection.execute("SELECT 1 FROM entitlements WHERE order_id=?", (order_id,)).fetchone()):
                raise BusinessError("order_already_issued", "This order already has an authorization.", 409)
            rights = connection.execute(f"""SELECT id, source_code_id, order_id, product_id, course_id, issued_policy_json,
                revoked_at, purpose FROM entitlements WHERE source_code_id IN ({placeholders})""", ids).fetchall()
            if (len(rights) > 1 or bool(rights) != bool(code["used_at"]) or (rights and (
                rights[0]["source_code_id"] != code["id"] or rights[0]["order_id"] is not None or rights[0]["revoked_at"]
                or rights[0]["purpose"] != "sale" or CodeService._policy(rights[0]) != policy))):
                raise BusinessError("association_conflict", "Verify the original entitlement and its unique order association.", 409)
            now = to_db_time(self.clock())
            connection.execute(f"""UPDATE access_codes SET order_id=?, verified_at=?, verified_by=?, verified_reason=?,
                revision=revision+1 WHERE id IN ({placeholders})""", (order_id, now, actor.admin_id, reason, *ids))
            for batch_id in {item["batch_id"] for item in chain}:
                connection.execute("UPDATE code_batches SET revision=revision+1 WHERE id=?", (batch_id,))
            if rights:
                connection.execute("""UPDATE entitlements SET order_id=?, verified_at=?, verified_by=?, verified_reason=?,
                    revision=revision+1 WHERE id=?""", (order_id, now, actor.admin_id, reason, rights[0]["id"]))
            connection.execute("UPDATE orders SET delivery_state=?, revision=revision+1 WHERE id=?",
                ("activated" if rights else "code_ready", order_id))
            self._save(connection, actor, "order.attach", key, digest, order_id)
            append_event(connection, self._event(actor, "order.attach", order_id, request_id=actor.request_id, changes={"revision": revision + 1}))
            return _record(self._row(connection, order_id))

    def record_refund(self, actor: Actor, order_id: int, revision: int, reason: str,
                      idempotency_key: str) -> OrderRecord:
        with self._write(actor, "order.refund", order_id) as connection:
            CodeService._identity(order_id)
            reason = self.entitlements._reason(reason)
            replay, key, digest = CodeService._request(connection, actor, "order.refund", idempotency_key,
                {"order_id": order_id, "revision": revision, "reason": reason})
            if replay:
                return _record(self._row(connection, order_id))
            row = self._row(connection, order_id)
            CodeService._revision(row, revision)
            self._paid(row)
            chain = self._chain(connection, order_id=order_id)
            if any(item["order_id"] not in (None, order_id) for item in chain):
                raise BusinessError("association_conflict", "Resolve inconsistent order ownership before recording refund.", 409)
            ids = [item["id"] for item in chain]
            now = to_db_time(self.clock())
            for item in chain:
                if not item["used_at"] and not item["voided_at"]:
                    self.codes._void(connection, item, reason)
            for batch_id in {item["batch_id"] for item in chain if item["batch_id"] is not None}:
                connection.execute("UPDATE code_batches SET revision=revision+1 WHERE id=?", (batch_id,))
            placeholders = ",".join("?" for _ in ids) or "NULL"
            rights = connection.execute(f"SELECT id, order_id FROM entitlements WHERE order_id=? OR source_code_id IN ({placeholders})", (order_id, *ids)).fetchall()
            if any(right["order_id"] not in (None, order_id) for right in rights):
                raise BusinessError("association_conflict", "Resolve inconsistent entitlement ownership before recording refund.", 409)
            for right in rights:
                self.entitlements.revoke_in_tx(connection, actor, right["id"], reason)
            connection.execute("UPDATE orders SET status='refunded', refunded_at=?, refund_reason=?, revision=revision+1 WHERE id=?",
                (now, reason, order_id))
            self._save(connection, actor, "order.refund", key, digest, order_id)
            append_event(connection, self._event(actor, "order.refund", order_id, request_id=actor.request_id,
                changes={"status": "refunded", "revision": revision + 1}))
            return _record(self._row(connection, order_id))

    def get_order(self, order_id: int) -> OrderRecord:
        with closing(open_readonly(self.db_path)) as connection:
            return _record(self._row(connection, order_id))

    def get_issue_replay(self, actor: Actor, order_id: int, revision: int, idempotency_key: str) -> BatchReceipt | None:
        """HTTP may check safe completed metadata before first-delivery configuration.

        Issuance repeats this check under its writer lock; this read cannot mint
        codes, change state or bypass owner/request-digest checks.
        """
        with closing(open_readonly(self.db_path)) as connection:
            CodeService._owner(connection, actor)
            CodeService._identity(order_id)
            replay, _, _ = CodeService._request(connection, actor, "order.issue", idempotency_key,
                {"order_id": order_id, "revision": revision})
            return replay

    def get_policy(self, order_id: int) -> IssuedPolicy:
        with closing(open_readonly(self.db_path)) as connection:
            self._row(connection, order_id)
            return self._policy(connection, order_id)

    def list_orders(self, *, page: int = 1, channel: str = "", shop_id: str = "", status: str = "", external_order_id: str = ""):
        offset = _page_offset(page)
        if (not isinstance(channel, str) or not isinstance(shop_id, str) or len(channel) > 200 or len(shop_id) > 200
            or status not in ("", "recorded", "code_ready", "delivered", "activated", "refunded")):
            raise BusinessError("invalid_filter", "Invalid order filter.", 400)
        if not isinstance(external_order_id, str) or len(external_order_id) > 300:
            raise BusinessError("invalid_filter", "Invalid order search.", 400)
        where, values = [], []
        for column, value in (("channel", channel.strip()), ("shop", shop_id.strip())):
            if value:
                where.append(f"{column}=?")
                values.append(value)
        if external_order_id.strip():
            where.append("external_order_id=?")
            values.append(external_order_id.strip())
        if status:
            where.append("status='refunded'" if status == "refunded" else "status='paid' AND delivery_state=?")
            if status != "refunded":
                values.append(status)
        clause = " WHERE " + " AND ".join(where) if where else ""
        with closing(open_readonly(self.db_path)) as connection:
            total = connection.execute("SELECT count(*) FROM orders" + clause, values).fetchone()[0]
            # Full external IDs are never loaded by a list/query projection.
            rows = connection.execute("""SELECT id, channel, shop AS shop_id, product_id, amount_cents, paid_at,
                status, delivery_state, revision FROM orders""" + clause + " ORDER BY id DESC LIMIT 20 OFFSET ?", (*values, offset)).fetchall()
            return [dict(row) for row in rows], total

    def get_detail(self, order_id: int, *, page: int = 1):
        offset = _page_offset(page)
        with closing(open_readonly(self.db_path)) as connection:
            order = _record(self._row(connection, order_id))
            times = dict(connection.execute("SELECT delivered_at, refunded_at, refund_reason FROM orders WHERE id=?", (order_id,)).fetchone())
            if times["refund_reason"]:
                try:
                    self.entitlements._reason(times["refund_reason"])
                    if re.search(r"(?<![A-Za-z0-9_-])[0-9a-fA-F]{64}(?![A-Za-z0-9_-])", times["refund_reason"]):
                        raise ValueError
                except (BusinessError, ValueError):
                    times["refund_reason"] = "退款核验记录含敏感信息，请重新核对来源。"
            policy = self._policy(connection, order_id)
            codes = [dict(row) for row in connection.execute("""SELECT c.code_number, c.batch_id, c.used_at, c.voided_at,
                c.expires_at, c.replaces_code_id, e.id AS entitlement_id FROM access_codes c
                LEFT JOIN entitlements e ON e.source_code_id=c.id WHERE c.order_id=? ORDER BY c.id LIMIT 20 OFFSET ?""", (order_id, offset))]
            total = connection.execute("SELECT count(*) FROM access_codes WHERE order_id=?", (order_id,)).fetchone()[0]
            return order, policy, times, codes, total
