"""Recovery owns transactions; issued rights own authorization and device limits."""

from __future__ import annotations

import re
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from ..audit import append_event
from ..database import from_db_time, open_readonly, to_db_time, transaction
from ..domain import Actor, BusinessError, Clock, utc_now
from ..operations.codes import CodeService
from ..operations.products import IssuedPolicy
from .entitlements import EntitlementService, SessionGrant, _hash


@dataclass(frozen=True)
class CredentialReceipt:
    entitlement_id: int
    raw_key: str | None = field(repr=False)
    replayed: bool


def _denied():
    return BusinessError("recovery_denied", "Learning credential is unavailable.", 403)


class RecoveryService:
    def __init__(self, db_path: Path, *, clock: Clock = utc_now, session_ttl_hours: int = 72,
                 entitlement_service: EntitlementService | None = None):
        self.db_path = Path(db_path)
        self.clock = clock
        self.entitlements = entitlement_service or EntitlementService(
            self.db_path, clock=clock, session_ttl_hours=session_ttl_hours)
        if (type(session_ttl_hours) is not int or session_ttl_hours <= 0
            or self.entitlements.db_path != self.db_path or self.entitlements.clock != clock
            or self.entitlements.session_ttl_hours != session_ttl_hours):
            raise ValueError("Recovery must share the entitlement database, clock and validated session lifetime.")

    def record_denial(self, actor, action, entitlement_id, error, *, request_id):
        if getattr(error, "denial_recorded", False):
            return
        if action not in ("recovery.recover", "recovery.reset", "entitlement.revoke"):
            raise ValueError("Unsupported recovery audit action.")
        try:
            with transaction(self.db_path, immediate=True) as connection:
                if actor and not connection.execute("SELECT 1 FROM admins WHERE id=?", (actor.admin_id,)).fetchone():
                    actor = None
                event = self.entitlements._event(actor, action, entitlement_id,
                    request_id=request_id, error=error)
                # The writer boundary makes lookup+append durable across service
                # and HTTP exception instances without storing request payloads.
                if not connection.execute("""SELECT 1 FROM admin_events WHERE action=? AND object_type='entitlement'
                    AND object_id=? AND request_id=? AND outcome='denied'""",
                    (action, event.object_id, event.request_id)).fetchone():
                    append_event(connection, event)
            error.denial_recorded = True
        except sqlite3.DatabaseError:
            raise BusinessError("audit_unavailable", "Unable to record the operation outcome.", 503) from None

    def restore(self, raw_key: str, *, evict_oldest: bool = False, request_id: str) -> SessionGrant:
        entitlement_id = None
        try:
            with transaction(self.db_path, immediate=True) as connection:
                if (not isinstance(raw_key, str) or not re.fullmatch(r"LK-[A-Za-z0-9_-]{43}", raw_key)
                    or type(evict_oldest) is not bool or not isinstance(request_id, str)
                    or not 1 <= len(request_id) <= 1000):
                    raise _denied()
                credential = connection.execute("""SELECT r.entitlement_id FROM recovery_credentials r
                    JOIN entitlements e ON e.id=r.entitlement_id
                    WHERE r.credential_hash=? AND r.revoked_at IS NULL AND e.issued_policy_json IS NOT NULL
                    AND (e.legacy_state IS NULL OR e.legacy_state!='pending_verification')""", (_hash(raw_key),)).fetchone()
                if not credential:
                    raise _denied()
                entitlement_id = credential["entitlement_id"]
                grant = self.entitlements.create_session_in_tx(connection, entitlement_id, evict_oldest=evict_oldest)
                append_event(connection, self.entitlements._event(None, "recovery.recover", entitlement_id,
                    request_id=request_id))
                return grant
        except (BusinessError, ValidationError, sqlite3.DatabaseError, ValueError, TypeError, OverflowError) as exc:
            error = BusinessError("device_confirmation_required",
                "Enter your credential again and confirm signing out the oldest device.", 409) if (
                isinstance(exc, BusinessError) and exc.code == "session_limit") else _denied()
            self.record_denial(None, "recovery.recover", entitlement_id, error, request_id=request_id)
            raise error from None

    @staticmethod
    def _verification(connection, row, policy):
        if policy is None or row["legacy_state"] == "pending_verification":
            raise BusinessError("verification_required", "Verify the original entitlement before resetting credentials.", 409)
        source = connection.execute("""SELECT c.purpose, c.product_id, c.course_id, c.order_id,
            c.issued_policy_json, c.used_at, c.voided_at, c.created_by, c.verified_at, c.verified_by,
            c.verified_reason, b.purpose AS batch_purpose, b.created_by AS batch_creator,
            b.issued_policy_json AS batch_policy
            FROM access_codes c JOIN code_batches b ON b.id=c.batch_id WHERE c.id=?""",
            (row["source_code_id"],)).fetchone()
        if (not source or source["purpose"] != row["purpose"] or source["batch_purpose"] != row["purpose"]
            or source["product_id"] != row["product_id"] or source["course_id"] != row["course_id"]
            or source["order_id"] != row["order_id"] or not source["used_at"] or source["voided_at"]
            or IssuedPolicy.model_validate_json(source["issued_policy_json"] or "null") != policy):
            raise BusinessError("verification_required", "Verify the original issuance purpose before resetting credentials.", 409)
        if row["purpose"] in ("test", "gift"):
            return
        if row["purpose"] != "sale":
            raise BusinessError("verification_required", "Verify the original entitlement before resetting credentials.", 409)
        if (not source["created_by"] or source["created_by"] != source["batch_creator"]
            or source["created_by"] != row["created_by"]
            or not connection.execute("SELECT 1 FROM admins WHERE id=? AND role='owner'", (source["created_by"],)).fetchone()
            or IssuedPolicy.model_validate_json(source["batch_policy"] or "null") != policy
            or (source["verified_at"], source["verified_by"], source["verified_reason"]) != (
                row["verified_at"], row["verified_by"], row["verified_reason"])):
            raise BusinessError("purchase_verification_required", "Original sale issuer and recorded code/entitlement proof must agree.", 409)
        order = connection.execute("""SELECT product_id, status, paid_at, issued_policy_json
            FROM orders WHERE id=?""", (row["order_id"],)).fetchone()
        if (not order or order["status"] != "paid" or order["product_id"] != row["product_id"]
            or not order["paid_at"] or not row["verified_at"] or not row["verified_by"]
            or not row["verified_reason"] or not row["verified_reason"].strip()
            or not connection.execute("SELECT 1 FROM admins WHERE id=? AND role='owner'", (row["verified_by"],)).fetchone()
            or not order["issued_policy_json"]):
            raise BusinessError("purchase_verification_required", "Recorded verified purchase and bound paid order evidence are required.", 409)
        if (from_db_time(order["paid_at"]) > from_db_time(row["verified_at"])
            or IssuedPolicy.model_validate_json(order["issued_policy_json"]) != policy):
            raise BusinessError("purchase_verification_required", "Recorded purchase evidence does not match the issued entitlement.", 409)

    def reset(self, actor: Actor, entitlement_id: int, revision: int, reason: str,
              idempotency_key: str) -> CredentialReceipt:
        try:
            with transaction(self.db_path, immediate=True) as connection:
                CodeService._owner(connection, actor)
                CodeService._identity(entitlement_id)
                reason = self.entitlements._reason(reason)
                replay, key_hash, digest = CodeService._request(connection, actor, "recovery.reset", idempotency_key,
                    {"entitlement_id": entitlement_id, "revision": revision, "reason": reason})
                if replay:
                    return CredentialReceipt(entitlement_id, None, True)
                row, policy = self.entitlements._entitlement(connection, entitlement_id)
                CodeService._revision(row, revision)
                self._verification(connection, row, policy)
                if row["purpose"] == "sale" and from_db_time(row["verified_at"]) > self.clock():
                    raise BusinessError("purchase_verification_required", "Purchase verification must already have occurred.", 409)
                now = to_db_time(self.clock())
                connection.execute("""UPDATE recovery_credentials SET revoked_at=?, revision=revision+1
                    WHERE entitlement_id=? AND revoked_at IS NULL""", (now, entitlement_id))
                revoked = connection.execute("UPDATE sessions SET revoked_at=? WHERE entitlement_id=? AND revoked_at IS NULL",
                                             (now, entitlement_id)).rowcount
                raw_key = "LK-" + secrets.token_urlsafe(32)
                connection.execute("""INSERT INTO recovery_credentials
                    (entitlement_id, credential_hash, created_at, created_by) VALUES (?, ?, ?, ?)""",
                    (entitlement_id, _hash(raw_key), now, actor.admin_id))
                if connection.execute("UPDATE entitlements SET revision=revision+1 WHERE id=? AND revision=?",
                                      (entitlement_id, revision)).rowcount != 1:
                    raise BusinessError("stale_revision", "This form is stale; reload and try again.", 409)
                connection.execute("""INSERT INTO operation_requests
                    (actor_admin_id, action, idempotency_key, request_digest, object_type, object_id, created_at)
                    VALUES (?, 'recovery.reset', ?, ?, 'entitlement', ?, ?)""",
                    (actor.admin_id, key_hash, digest, str(entitlement_id), now))
                append_event(connection, self.entitlements._event(actor, "recovery.reset", entitlement_id,
                    request_id=actor.request_id, changes={"revision": revision + 1, "revoked_sessions": revoked}))
                return CredentialReceipt(entitlement_id, raw_key, False)
        except (BusinessError, ValidationError, sqlite3.DatabaseError, ValueError, TypeError, OverflowError) as exc:
            error = exc if isinstance(exc, BusinessError) else BusinessError(
                "recovery_conflict", "Unable to reset credentials; verify the original records.", 409)
            self.record_denial(actor, "recovery.reset", entitlement_id, error, request_id=actor.request_id)
            raise error from None

    def get_detail(self, entitlement_id: int):
        """Private support projection: no credential/session hashes or history."""
        CodeService._identity(entitlement_id)
        with closing(open_readonly(self.db_path)) as connection:
            row = connection.execute("""SELECT id, course_id, product_id, order_id, source_code_id,
                course_version, expires_at, revoked_at, legacy_state, purpose, verified_at, verified_by,
                verified_reason, revision, created_at, issued_policy_json FROM entitlements WHERE id=?""",
                (entitlement_id,)).fetchone()
            if not row:
                raise BusinessError("entitlement_missing", "Entitlement is unavailable.", 404)
            detail = dict(row)
            # Historical verification notes were not necessarily written by
            # today's validators. Do not echo a credential or hash as history.
            if detail["verified_reason"]:
                try:
                    self.entitlements._reason(detail["verified_reason"])
                    if re.search(r"(?<![A-Za-z0-9_-])[0-9a-fA-F]{64}(?![A-Za-z0-9_-])", detail["verified_reason"]):
                        raise BusinessError("unsafe_history", "Unsafe history.", 400)
                except BusinessError:
                    detail["verified_reason"] = "核验记录含敏感信息，请重新核对来源。"
            snapshot = detail.pop("issued_policy_json")
            policy = None
            if snapshot:
                try:
                    issued = IssuedPolicy.model_validate_json(snapshot)
                    policy = {"title": issued.title, "support_text": issued.support_text,
                              "version": issued.version, **issued.access.model_dump()}
                except ValidationError:
                    pass
            detail["policy"] = policy
            detail["active_sessions"] = connection.execute("""SELECT count(*) FROM sessions WHERE entitlement_id=?
                AND revoked_at IS NULL AND expires_at>?""", (entitlement_id, to_db_time(self.clock()))).fetchone()[0]
            detail["progress"] = [dict(row) for row in connection.execute("""SELECT chapter_number, completed,
                updated_at FROM entitlement_progress WHERE entitlement_id=? ORDER BY chapter_number""", (entitlement_id,))]
            return detail
