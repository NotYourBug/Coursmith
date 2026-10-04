"""Explicit owner evidence for preserved historical codes and individual rights."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from ..audit import AuditEvent, append_event, record_denial
from ..content_inspection import inspect_package
from ..database import from_db_time, open_readonly, to_db_time, transaction
from ..domain import Actor, BusinessError, Clock, utc_now
from .codes import CodeService, _hash, _json
from .products import AccessPolicy, IssuedPolicy, ProductService, _page_offset, _record


def _required():
    return BusinessError("verification_required", "Verify the preserved original ownership and explicit policy first.", 409)


def _reason(value):
    value = CodeService._reason(value)
    if re.search(r"(?<![A-Za-z0-9_-])(?:[A-Za-z0-9_-]{43}|[0-9a-fA-F]{64})(?![A-Za-z0-9_-])", value):
        raise BusinessError("invalid_reason", "Provide verification evidence without credentials or hashes.", 400)
    return value


def _evidence(connection, row, *, kind):
    column = "code_id" if kind == "code" else "entitlement_id"
    proof = connection.execute(f"SELECT * FROM legacy_verifications WHERE {column}=?", (row["id"],)).fetchone()
    if (not proof or row["legacy_state"] not in ("resolved", "verified")
        or (proof["purpose"], proof["order_id"], proof["issued_policy_json"], proof["expires_at"],
            proof["verified_at"], proof["actor_admin_id"], proof["reason"]) !=
           (row["purpose"], row["order_id"], row["issued_policy_json"], row["expires_at"],
            row["verified_at"], row["verified_by"], row["verified_reason"])):
        raise _required()
    _reason(proof["reason"])
    # Historical attribution survives account disable; new owner writes still
    # require CodeService._owner's enabled check in their own transaction.
    owner = connection.execute("SELECT 1 FROM admins WHERE id=? AND role='owner'",
        (proof["actor_admin_id"],)).fetchone()
    audit = connection.execute("""SELECT 1 FROM admin_events WHERE id=? AND actor_admin_id=?
        AND action='legacy.verify' AND object_type=? AND object_id=? AND outcome='success'
        AND json_extract(changes_json, '$.revision')=?""",
        (proof["audit_event_id"], proof["actor_admin_id"], kind, str(row["id"]), proof["revision"])).fetchone()
    if not owner or not audit:
        raise _required()
    return proof


def verified_code_in_tx(connection, code, now):
    """Bounded compatibility: migration origin + approved transaction, not hash alone."""
    origin = connection.execute("SELECT * FROM legacy_code_origins WHERE code_id=?", (code["id"],)).fetchone()
    proof = _evidence(connection, code, kind="code")
    if (not origin or not origin["content_available"] or origin["original_used_at"] is not None
        or code["batch_id"] is not None or code["created_by"] is not None
        or code["code_hash"] != origin["code_hash"] or code["created_at"] != origin["original_created_at"]
        or proof["original_expires_at"] != origin["original_expires_at"]
        or (origin["original_expires_at"] is not None and code["expires_at"] != origin["original_expires_at"])
        or (code["course_version"], code["package_hash"]) != (origin["course_version"], origin["package_hash"])
        or from_db_time(proof["verified_at"]) > now):
        raise _required()
    return proof


def verified_entitlement_in_tx(connection, row, policy, now):
    proof = _evidence(connection, row, kind="entitlement")
    origin = connection.execute("SELECT * FROM legacy_entitlement_origins WHERE entitlement_id=?", (row["id"],)).fetchone()
    if origin:
        session = connection.execute("SELECT entitlement_id, source_code_id FROM sessions WHERE session_hash=?",
            (origin["session_hash"],)).fetchone()
        valid = (origin["content_available"] and session and session["entitlement_id"] == row["id"]
            and session["source_code_id"] is None and row["source_code_id"] is None
            and proof["original_expires_at"] == origin["original_expires_at"]
            and (row["course_version"], row["package_hash"], row["created_at"]) ==
                (origin["course_version"], origin["package_hash"], origin["original_created_at"]))
    else:
        code = connection.execute("SELECT * FROM access_codes WHERE id=?", (row["source_code_id"],)).fetchone()
        if not code:
            raise _required()
        verified_code_in_tx(connection, code, now)
        valid = (code["used_at"] and code["issued_policy_json"] == row["issued_policy_json"]
            and code["course_id"] == row["course_id"] and code["purpose"] == row["purpose"])
    if not valid or row["created_by"] is not None or from_db_time(proof["verified_at"]) > now:
        raise _required()
    if row["purpose"] == "sale":
        _verified_order(connection, row["order_id"], policy, from_db_time(proof["verified_at"]), row["id"])
    elif row["purpose"] not in ("test", "gift") or row["order_id"] is not None:
        raise _required()
    return proof


def _verified_order(connection, order_id, policy, now, entitlement_id):
    CodeService._identity(order_id)
    order = connection.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if (not order or order["status"] != "paid" or not order["paid_at"]
        or order["product_id"] != policy.product_id or from_db_time(order["paid_at"]) > now
        or IssuedPolicy.model_validate_json(order["issued_policy_json"] or "null") != policy
        or not connection.execute("SELECT 1 FROM admins WHERE id=? AND role='owner'", (order["created_by"],)).fetchone()
        or not connection.execute("""SELECT 1 FROM admin_events WHERE action='order.create' AND outcome='success'
            AND actor_admin_id=? AND object_type='order' AND object_id=?""", (order["created_by"], str(order_id))).fetchone()
        or connection.execute("SELECT 1 FROM access_codes WHERE order_id=?", (order_id,)).fetchone()
        or connection.execute("SELECT 1 FROM entitlements WHERE order_id=? AND id!=?", (order_id, entitlement_id)).fetchone()):
        raise BusinessError("purchase_verification_required", "Choose an unbound, owner-recorded paid order matching the original policy.", 409)
    return order


class LegacyService:
    def __init__(self, db_path: Path, *, clock: Clock = utc_now):
        self.db_path, self.clock = Path(db_path), clock
        self.products = ProductService(self.db_path, clock=clock)

    def record_denial(self, actor, kind, identity, error, *, request_id):
        if getattr(error, "denial_recorded", False):
            return
        if actor:
            with closing(open_readonly(self.db_path)) as connection:
                if not connection.execute("SELECT 1 FROM admins WHERE id=?", (actor.admin_id,)).fetchone():
                    actor = None
        try:
            record_denial(self.db_path, AuditEvent(actor.admin_id if actor else None, kind,
                str(identity) if type(identity) is int and identity > 0 else "invalid", "legacy.verify",
                error.code, "denied", _hash(request_id), {"error_code": error.code}))
        except sqlite3.DatabaseError:
            raise BusinessError("audit_unavailable", "Unable to record the operation outcome.", 503) from None
        error.denial_recorded = True

    @contextmanager
    def _write(self, actor, kind, identity):
        try:
            with transaction(self.db_path, immediate=True) as connection:
                CodeService._owner(connection, actor)
                CodeService._identity(identity)
                yield connection
        except (BusinessError, ValidationError, sqlite3.DatabaseError, ValueError, TypeError, OverflowError) as exc:
            error = exc if isinstance(exc, BusinessError) else BusinessError("legacy_conflict", "Unable to verify the original records.", 409)
            self.record_denial(actor, kind, identity, error, request_id=actor.request_id)
            raise error from None

    @staticmethod
    def _input(policy, purpose, reason):
        if purpose not in ("sale", "test", "gift"):
            raise BusinessError("invalid_purpose", "Choose an explicit verified purpose.", 400)
        return AccessPolicy.model_validate(policy.model_dump()) if policy is not None else None, _reason(reason)

    def _save(self, connection, actor, row, kind, policy, expiry, purpose, reason, order_id):
        now = to_db_time(self.clock())
        append_event(connection, AuditEvent(actor.admin_id, kind, str(row["id"]), "legacy.verify", "completed",
            "success", _hash(actor.request_id), {"revision": row["revision"] + 1}))
        audit_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute(f"""INSERT INTO legacy_verifications
            ({'code_id' if kind == 'code' else 'entitlement_id'}, actor_admin_id, audit_event_id, purpose,
             order_id, issued_policy_json, original_expires_at, expires_at, reason, verified_at, revision)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (row["id"], actor.admin_id, audit_id,
                purpose, order_id, _json(policy.model_dump()) if policy else None, row["expires_at"], expiry,
                reason, now, row["revision"] + 1))
        return now

    def resolve_code(self, actor: Actor, code_id: int, revision: int, policy: AccessPolicy | None,
                     purpose: Literal['sale', 'test', 'gift'], verification_reason: str, *, activation_days: int | None = None) -> None:
        with self._write(actor, "code", code_id) as connection:
            policy, reason = self._input(policy, purpose, verification_reason)
            row = connection.execute("SELECT * FROM access_codes WHERE id=?", (code_id,)).fetchone()
            origin = connection.execute("SELECT * FROM legacy_code_origins WHERE code_id=?", (code_id,)).fetchone()
            if not row or not origin or row["legacy_state"] != "pending_verification":
                raise _required()
            CodeService._revision(row, revision)
            if (row["code_hash"], row["created_at"], row["expires_at"], row["used_at"]) != (
                origin["code_hash"], origin["original_created_at"], origin["original_expires_at"], origin["original_used_at"]):
                raise _required()
            issued, expiry = None, row["expires_at"]
            if row["used_at"] is not None:
                if policy is not None or activation_days is not None:
                    raise BusinessError("used_legacy_code", "Used historical codes record purpose only; verify the chosen individual entitlement separately.", 409)
            else:
                if row["voided_at"] or not origin["content_available"] or policy is None:
                    raise _required()
                issued = self.products.require_sale_ready_in_tx(connection, row["product_id"])
                if issued.access != policy or (issued.version, issued.package_hash) != (origin["course_version"], origin["package_hash"]):
                    raise BusinessError("policy_mismatch", "Explicit policy must match the approved original course product.", 409)
                if expiry is None:
                    if type(activation_days) is not int or not 1 <= activation_days <= 365:
                        raise BusinessError("activation_days_required", "Explicitly choose 1–365 activation days for a historical code without a deadline.", 400)
                    expiry = to_db_time(self.clock() + timedelta(days=activation_days))
                elif activation_days is not None or from_db_time(expiry) <= self.clock():
                    raise BusinessError("activation_deadline", "Preserve the existing valid activation deadline.", 409)
            now = self._save(connection, actor, row, "code", issued, expiry, purpose, reason, row["order_id"])
            connection.execute("""UPDATE access_codes SET legacy_state='resolved', purpose=?, verified_at=?,
                verified_by=?, verified_reason=?, expires_at=?, issued_policy_json=?, access_days=?, update_policy=?,
                course_version=?, package_hash=?, revision=revision+1 WHERE id=?""", (purpose, now, actor.admin_id,
                reason, expiry, _json(issued.model_dump()) if issued else None, issued.access.access_days if issued else row["access_days"],
                issued.access.update_policy if issued else row["update_policy"], issued.version if issued else row["course_version"],
                issued.package_hash if issued else row["package_hash"], code_id))

    def _policy(self, connection, row, policy):
        course = connection.execute("SELECT * FROM courses WHERE course_id=?", (row["course_id"],)).fetchone()
        package = inspect_package(Path(course["content_path"]))
        if ((package.course_id, package.slug, package.version, package.fingerprint) !=
            (row["course_id"], course["slug"], row["course_version"], row["package_hash"])
            or course["version"] != row["course_version"]
            or (policy.pdf and not package.pdf_ready) or (policy.zip and not package.zip_ready)
            or not any((policy.online, policy.pdf, policy.zip))):
            raise BusinessError("legacy_content_unavailable", "The preserved original course and chosen formats are unavailable.", 409)
        if row["issued_policy_json"]:
            issued = IssuedPolicy.model_validate_json(row["issued_policy_json"])
            if issued.access != policy:
                raise BusinessError("policy_mismatch", "Preserve the original resolved code policy.", 409)
            return issued
        product = self.products._row(connection, row["product_id"])
        data = _record(product).data
        return IssuedPolicy(product_id=product["id"], course_id=row["course_id"], course_slug=course["slug"],
            version=row["course_version"], package_hash=row["package_hash"], access=policy,
            title=data.title, support_text=data.support_text)

    def verify_entitlement(self, actor: Actor, entitlement_id: int, revision: int, order_id: int | None,
                           policy: AccessPolicy, expires_at: datetime | None, purpose: Literal['sale','test','gift'],
                           verification_reason: str, idempotency_key: str) -> None:
        with self._write(actor, "entitlement", entitlement_id) as connection:
            policy, reason = self._input(policy, purpose, verification_reason)
            if policy is None or (expires_at is not None and (not isinstance(expires_at, datetime) or
                expires_at.tzinfo is None or expires_at.utcoffset() is None)):
                raise BusinessError("explicit_deadline", "Choose an explicit policy and UTC-offset deadline.", 400)
            expiry = to_db_time(expires_at) if expires_at is not None else None
            replay, key, digest = CodeService._request(connection, actor, "legacy.verify", idempotency_key,
                dict(entitlement_id=entitlement_id, revision=revision, order_id=order_id, policy=policy.model_dump(),
                    expires_at=expiry, purpose=purpose, verification_reason=reason))
            if replay:
                return
            row = connection.execute("SELECT * FROM entitlements WHERE id=?", (entitlement_id,)).fetchone()
            origin = connection.execute("SELECT * FROM legacy_entitlement_origins WHERE entitlement_id=?", (entitlement_id,)).fetchone()
            if not row or row["legacy_state"] not in ("pending_verification", "resolved_code") or row["revoked_at"]:
                raise _required()
            CodeService._revision(row, revision)
            if origin:
                if (not origin["content_available"] or row["source_code_id"] is not None
                    or row["expires_at"] != origin["original_expires_at"]):
                    raise _required()
            else:
                code = connection.execute("SELECT * FROM access_codes WHERE id=?", (row["source_code_id"],)).fetchone()
                if not code or not code["used_at"]:
                    raise _required()
                verified_code_in_tx(connection, code, self.clock())
                if code["purpose"] != purpose:
                    raise BusinessError("purpose_mismatch", "Preserve the explicitly verified original code purpose.", 409)
            issued = self._policy(connection, row, policy)
            if ((policy.access_mode == "no_fixed_expiry" and expiry is not None)
                or (policy.access_mode == "days" and (expiry is None or expires_at <= self.clock()
                    or expires_at > self.clock() + timedelta(days=3650)))):
                raise BusinessError("explicit_deadline", "Confirm a future explicit learning deadline, or null only for explicitly chosen no-fixed-expiry.", 400)
            if purpose == "sale":
                order = _verified_order(connection, order_id, issued, self.clock(), entitlement_id)
            elif order_id is not None:
                raise BusinessError("order_purpose", "Test/gift verification cannot claim a paid order.", 400)
            now = self._save(connection, actor, row, "entitlement", issued, expiry, purpose, reason, order_id)
            connection.execute("""UPDATE entitlements SET order_id=?, access_days=?, issued_policy_json=?,
                expires_at=?, purpose=?, verified_at=?, verified_by=?, verified_reason=?, legacy_state='verified',
                revision=revision+1 WHERE id=?""", (order_id, policy.access_days, _json(issued.model_dump()), expiry,
                    purpose, now, actor.admin_id, reason, entitlement_id))
            if purpose == "sale":
                connection.execute("UPDATE orders SET delivery_state='activated', revision=revision+1 WHERE id=?", (order["id"],))
            connection.execute("""INSERT INTO operation_requests
                (actor_admin_id, action, idempotency_key, request_digest, object_type, object_id, created_at)
                VALUES (?, 'legacy.verify', ?, ?, 'entitlement', ?, ?)""",
                (actor.admin_id, key, digest, str(entitlement_id), now))

    def list_codes(self, *, page: int = 1):
        with closing(open_readonly(self.db_path)) as connection:
            total = connection.execute("SELECT count(*) FROM legacy_code_origins").fetchone()[0]
            codes = [dict(row) for row in connection.execute("""SELECT c.id, c.course_id, c.product_id, c.revision,
                c.created_at, c.expires_at, c.used_at, c.legacy_state, c.purpose FROM access_codes c
                JOIN legacy_code_origins o ON o.code_id=c.id ORDER BY c.id DESC LIMIT 20 OFFSET ?""", (_page_offset(page),))]
            return codes, total

    def list_entitlements(self, *, page: int = 1):
        with closing(open_readonly(self.db_path)) as connection:
            total = connection.execute("SELECT count(*) FROM entitlements WHERE legacy_state IS NOT NULL").fetchone()[0]
            rights = [dict(row) for row in connection.execute("""SELECT id, course_id, revision, expires_at,
                legacy_state, purpose FROM entitlements WHERE legacy_state IS NOT NULL
                ORDER BY id LIMIT 20 OFFSET ?""", (_page_offset(page),))]
            return rights, total
