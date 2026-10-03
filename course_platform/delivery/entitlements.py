"""Persistent issued rights and transaction-owned buyer authorization."""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from ..audit import AuditEvent, append_event, record_denial
from ..content import CourseManifest
from ..content_inspection import read_verified_file
from ..database import from_db_time, open_readonly, to_db_time, transaction
from ..domain import Actor, BusinessError, Clock, utc_now
from ..operations.codes import CodeService
from ..operations.products import IssuedPolicy


@dataclass(frozen=True)
class SessionGrant:
    session_id: str = field(repr=False)
    csrf_token: str = field(repr=False)
    entitlement_id: int
    course_id: str
    session_expires_at: datetime


@dataclass(frozen=True)
class RedemptionReceipt:
    session: SessionGrant
    raw_recovery_key: str = field(repr=False)
    entitlement_expires_at: datetime | None


@dataclass(frozen=True)
class AuthorizedSession:
    session_hash: str
    entitlement_id: int
    course_id: str
    session_expires_at: datetime
    entitlement_expires_at: datetime | None
    csrf_hash: str | None
    issued_policy: IssuedPolicy | None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf8")).hexdigest()


def _denied(kind: str) -> BusinessError:
    return BusinessError(f"{kind}_denied", "Unable to redeem this code." if kind == "redemption"
                         else "Learning session is unavailable.", 403)


class EntitlementService:
    def __init__(self, db_path: Path, *, clock: Clock = utc_now, session_ttl_hours: int = 72):
        if type(session_ttl_hours) is not int or session_ttl_hours <= 0:
            raise ValueError("Session lifetime must be a positive integer.")
        self.db_path = Path(db_path)
        self.clock = clock
        self.session_ttl_hours = session_ttl_hours

    @staticmethod
    def _in_tx(connection: sqlite3.Connection) -> None:
        if not connection.in_transaction:
            raise BusinessError("entitlement_transaction", "An active writer transaction is required.", 409)
        # Acquire a deferred caller's writer boundary before authorization reads.
        try:
            connection.execute("UPDATE entitlements SET id=id WHERE 0")
        except sqlite3.DatabaseError:
            raise BusinessError("entitlement_transaction", "Unable to acquire the writer transaction.", 409) from None

    @staticmethod
    def _event(actor, action, object_id, *, request_id, error=None, changes=None):
        # Correlation input could accidentally be a credential: retain a digest.
        correlation = _hash(request_id if isinstance(request_id, str) else "invalid")
        return AuditEvent(actor_admin_id=actor.admin_id if actor else None,
            object_type="code" if action == "code.redeem" else "entitlement",
            object_id=str(object_id) if type(object_id) is int and object_id > 0 else "invalid",
            action=action, reason=error.code if error else "completed",
            outcome="denied" if error else "success", request_id=correlation,
            changes={"error_code": error.code} if error else (changes or {}))

    def record_denial(self, actor: Actor | None, action: str, entitlement_id, error: BusinessError,
                      *, request_id: str) -> None:
        """Owning callers invoke this only after rollback, including Task9."""
        if getattr(error, "denial_recorded", False):
            return
        try:
            if actor:
                with closing(open_readonly(self.db_path)) as connection:
                    if not connection.execute("SELECT 1 FROM admins WHERE id=?", (actor.admin_id,)).fetchone():
                        actor = None
            record_denial(self.db_path, self._event(actor, action, entitlement_id,
                                                  request_id=request_id, error=error))
        except sqlite3.DatabaseError:
            raise BusinessError("audit_unavailable", "Unable to record the operation outcome.", 503) from None
        error.denial_recorded = True

    @staticmethod
    def _policy(connection, row) -> IssuedPolicy | None:
        if row["issued_policy_json"] is None:
            # An issued entitlement losing its snapshot is corruption, not legacy.
            if row["legacy_state"] != "pending_verification" or row["created_by"] is not None:
                raise _denied("session")
            if row["source_code_id"] is not None:
                source = connection.execute("SELECT issued_policy_json, created_by FROM access_codes WHERE id=?",
                                            (row["source_code_id"],)).fetchone()
                if not source or source["issued_policy_json"] is not None or source["created_by"] is not None:
                    raise _denied("session")
            return None
        policy = IssuedPolicy.model_validate_json(row["issued_policy_json"])
        if (policy.product_id, policy.course_id, policy.version, policy.package_hash,
            policy.access.access_days, policy.access.update_policy) != (
            row["product_id"], row["course_id"], row["course_version"], row["package_hash"],
            row["access_days"], row["update_policy"]):
            raise _denied("session")
        return policy

    @staticmethod
    def _verify_content(connection, row, policy):
        course = connection.execute("SELECT * FROM courses WHERE course_id=?", (row["course_id"],)).fetchone()
        if not course or course["version"] != row["course_version"]:
            raise _denied("session")
        manifest = CourseManifest.model_validate_json(read_verified_file(
            Path(course["content_path"]), "manifest.json", row["package_hash"]))
        if (manifest.course_id, manifest.slug, manifest.version) != (
                row["course_id"], course["slug"], row["course_version"]):
            raise _denied("session")
        if policy and (policy.course_slug != course["slug"] or not policy.package_hash):
            raise _denied("session")

    def _entitlement(self, connection, entitlement_id):
        CodeService._identity(entitlement_id)
        row = connection.execute("SELECT * FROM entitlements WHERE id=?", (entitlement_id,)).fetchone()
        if not row or row["revoked_at"] or (row["expires_at"] and from_db_time(row["expires_at"]) <= self.clock()):
            raise _denied("session")
        policy = self._policy(connection, row)
        self._verify_content(connection, row, policy)
        return row, policy

    def redeem(self, raw_code: str, *, expected_course_id: str | None, request_id: str) -> RedemptionReceipt:
        code_id = None
        try:
            with transaction(self.db_path, immediate=True) as connection:
                if not isinstance(raw_code, str) or not re.fullmatch(r"CS-[A-Za-z0-9_-]{32}", raw_code):
                    raise _denied("redemption")
                if not isinstance(request_id, str) or not request_id or len(request_id) > 1000:
                    raise _denied("redemption")
                if expected_course_id is not None and (not isinstance(expected_course_id, str) or not expected_course_id):
                    raise _denied("redemption")
                codes = connection.execute("SELECT * FROM access_codes WHERE code_hash=?", (_hash(raw_code),)).fetchall()
                if len(codes) != 1:
                    raise _denied("redemption")
                code = codes[0]
                code_id = code["id"]
                now = self.clock()
                if (code["used_at"] or code["voided_at"] or not code["expires_at"]
                    or from_db_time(code["expires_at"]) <= now
                    or (expected_course_id is not None and code["course_id"] != expected_course_id)
                    or code["issued_policy_json"] is None):
                    raise _denied("redemption")
                # The full original code snapshot, never today's product policy.
                policy = IssuedPolicy.model_validate_json(code["issued_policy_json"])
                if (policy.product_id, policy.course_id, policy.version, policy.package_hash,
                    policy.access.access_days, policy.access.update_policy) != (
                    code["product_id"], code["course_id"], code["course_version"], code["package_hash"],
                    code["access_days"], code["update_policy"]):
                    raise _denied("redemption")
                self._verify_content(connection, code, policy)
                if code["order_id"] is not None:
                    order = connection.execute("SELECT * FROM orders WHERE id=?", (code["order_id"],)).fetchone()
                    if not order or order["status"] != "paid" or order["product_id"] != policy.product_id:
                        raise _denied("redemption")
                batch = connection.execute("SELECT revision FROM code_batches WHERE id=?", (code["batch_id"],)).fetchone()
                if not batch:
                    raise _denied("redemption")
                used = connection.execute("""UPDATE access_codes SET used_at=?
                    WHERE id=? AND used_at IS NULL AND voided_at IS NULL AND revision=?""",
                    (to_db_time(now), code_id, code["revision"])).rowcount
                bumped = connection.execute("UPDATE code_batches SET revision=revision+1 WHERE id=? AND revision=?",
                                            (code["batch_id"], batch["revision"])).rowcount
                if used != 1 or bumped != 1:
                    raise _denied("redemption")
                expires = now + timedelta(days=policy.access.access_days) if policy.access.access_days is not None else None
                entitlement_id = connection.execute("""INSERT INTO entitlements
                    (course_id, product_id, order_id, source_code_id, course_version, package_hash, access_days,
                     update_policy, expires_at, purpose, created_by, created_at, issued_policy_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (code["course_id"], code["product_id"], code["order_id"], code_id, policy.version, policy.package_hash,
                     policy.access.access_days, policy.access.update_policy, to_db_time(expires) if expires else None,
                     code["purpose"], code["created_by"], to_db_time(now), code["issued_policy_json"])).lastrowid
                recovery = "LK-" + secrets.token_urlsafe(32)
                connection.execute("INSERT INTO recovery_credentials (entitlement_id, credential_hash, created_at) VALUES (?, ?, ?)",
                                   (entitlement_id, _hash(recovery), to_db_time(now)))
                session = self.create_session_in_tx(connection, entitlement_id, evict_oldest=False)
                if code["order_id"] is not None:
                    updated = connection.execute("""UPDATE orders SET delivery_state='activated', revision=revision+1
                        WHERE id=? AND status='paid' AND revision=?""", (order["id"], order["revision"])).rowcount
                    if updated != 1:
                        raise _denied("redemption")
                append_event(connection, self._event(None, "code.redeem", code_id, request_id=request_id))
                return RedemptionReceipt(session, recovery, expires)
        except (BusinessError, ValidationError, sqlite3.DatabaseError, ValueError, TypeError, OverflowError):
            error = _denied("redemption")
            self.record_denial(None, "code.redeem", code_id, error, request_id=request_id)
            raise error from None

    def require_session(self, raw_token: str, course_id: str) -> AuthorizedSession:
        with transaction(self.db_path, immediate=True) as connection:
            return self.require_session_in_tx(connection, raw_token, course_id)

    def require_session_in_tx(self, connection: sqlite3.Connection, raw_token: str,
                              course_id: str) -> AuthorizedSession:
        """Generic rights authorization; consumers enforce online/download flags.

        Null policy means legacy online-only, never default download promises.
        Caller owns rollback and denial auditing; no nested service connection.
        """
        self._in_tx(connection)
        try:
            if not isinstance(raw_token, str) or not raw_token or len(raw_token) > 200 or not isinstance(course_id, str):
                raise _denied("session")
            session_hash = _hash(raw_token)
            session = connection.execute("SELECT * FROM sessions WHERE session_hash=?", (session_hash,)).fetchone()
            if (not session or session["course_id"] != course_id or session["entitlement_id"] is None
                or session["revoked_at"] or from_db_time(session["expires_at"]) <= self.clock()):
                raise _denied("session")
            row, policy = self._entitlement(connection, session["entitlement_id"])
            if policy is not None and not session["csrf_hash"]:
                raise _denied("session")
            if row["course_id"] != course_id or session["source_code_id"] != row["source_code_id"]:
                raise _denied("session")
            expiry = from_db_time(row["expires_at"]) if row["expires_at"] else None
            session_expiry = from_db_time(session["expires_at"])
            return AuthorizedSession(session_hash, row["id"], course_id,
                min(session_expiry, expiry) if expiry else session_expiry, expiry, session["csrf_hash"], policy)
        except (BusinessError, ValidationError, sqlite3.DatabaseError, ValueError, TypeError):
            raise _denied("session") from None

    def create_session_in_tx(self, connection: sqlite3.Connection, entitlement_id: int,
                             *, evict_oldest: bool) -> SessionGrant:
        """Recovery reuses this writer boundary, expiry and three-device algorithm."""
        self._in_tx(connection)
        try:
            if type(evict_oldest) is not bool:
                raise _denied("session")
            row, _ = self._entitlement(connection, entitlement_id)
            now = self.clock()
            active = [session for session in connection.execute("""SELECT session_hash, expires_at
                FROM sessions WHERE entitlement_id=? AND revoked_at IS NULL ORDER BY created_at, session_hash""", (entitlement_id,))
                if from_db_time(session["expires_at"]) > now]
            if len(active) >= 3:
                if not evict_oldest:
                    raise BusinessError("session_limit", "Confirm signing out the oldest device before continuing.", 409)
                for session in active[:len(active) - 2]:
                    connection.execute("UPDATE sessions SET revoked_at=? WHERE session_hash=?", (to_db_time(now), session["session_hash"]))
                append_event(connection, self._event(None, "session.revoke", entitlement_id,
                    request_id="device-limit", changes={"revoked_sessions": len(active) - 2}))
            expiry = now + timedelta(hours=self.session_ttl_hours)
            if row["expires_at"]:
                expiry = min(expiry, from_db_time(row["expires_at"]))
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            connection.execute("""INSERT INTO sessions
                (session_hash, course_id, created_at, expires_at, entitlement_id, source_code_id, csrf_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)""", (_hash(token), row["course_id"], to_db_time(now), to_db_time(expiry),
                    entitlement_id, row["source_code_id"], _hash(csrf)))
            return SessionGrant(token, csrf, entitlement_id, row["course_id"], expiry)
        except BusinessError:
            raise
        except (ValidationError, sqlite3.DatabaseError, ValueError, TypeError, OverflowError):
            raise _denied("session") from None

    @staticmethod
    def _reason(reason):
        reason = CodeService._reason(reason)
        if re.search(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])", reason):
            raise BusinessError("invalid_reason", "Provide a reason without credentials.", 400)
        return reason

    def revoke(self, actor: Actor, entitlement_id: int, revision: int, reason: str,
               idempotency_key: str) -> None:
        try:
            with transaction(self.db_path, immediate=True) as connection:
                CodeService._owner(connection, actor)
                CodeService._identity(entitlement_id)
                reason = self._reason(reason)
                replay, key_hash, digest = CodeService._request(connection, actor, "entitlement.revoke", idempotency_key,
                    {"entitlement_id": entitlement_id, "revision": revision, "reason": reason})
                if replay:
                    return
                row = connection.execute("SELECT revision, revoked_at FROM entitlements WHERE id=?", (entitlement_id,)).fetchone()
                if not row:
                    raise BusinessError("entitlement_missing", "Entitlement is unavailable.", 404)
                CodeService._revision(row, revision)
                if row["revoked_at"]:
                    raise BusinessError("entitlement_revoked", "Entitlement has already been revoked.", 409)
                self.revoke_in_tx(connection, actor, entitlement_id, reason)
                connection.execute("""INSERT INTO operation_requests
                    (actor_admin_id, action, idempotency_key, request_digest, object_type, object_id, created_at)
                    VALUES (?, 'entitlement.revoke', ?, ?, 'entitlement', ?, ?)""",
                    (actor.admin_id, key_hash, digest, str(entitlement_id), to_db_time(self.clock())))
        except (BusinessError, sqlite3.DatabaseError) as exc:
            error = exc if isinstance(exc, BusinessError) else BusinessError("entitlement_conflict", "Unable to revoke entitlement.", 409)
            self.record_denial(actor, "entitlement.revoke", entitlement_id, error, request_id=actor.request_id)
            raise error from None

    def revoke_in_tx(self, connection: sqlite3.Connection, actor: Actor, entitlement_id: int,
                     reason: str) -> None:
        """Caller must roll back on failure; this helper never opens or commits."""
        self._in_tx(connection)
        try:
            CodeService._owner(connection, actor)
            CodeService._identity(entitlement_id)
            reason = self._reason(reason)
            row = connection.execute("SELECT revision, revoked_at FROM entitlements WHERE id=?", (entitlement_id,)).fetchone()
            if not row:
                raise BusinessError("entitlement_missing", "Entitlement is unavailable.", 404)
            if row["revoked_at"]:
                return
            now = to_db_time(self.clock())
            connection.execute("UPDATE entitlements SET revoked_at=?, revoke_reason=?, revision=revision+1 WHERE id=?", (now, reason, entitlement_id))
            count = connection.execute("UPDATE sessions SET revoked_at=? WHERE entitlement_id=? AND revoked_at IS NULL", (now, entitlement_id)).rowcount
            connection.execute("UPDATE recovery_credentials SET revoked_at=?, revision=revision+1 WHERE entitlement_id=? AND revoked_at IS NULL", (now, entitlement_id))
            append_event(connection, self._event(actor, "entitlement.revoke", entitlement_id, request_id=actor.request_id,
                         changes={"revision": row["revision"] + 1, "revoked_sessions": count}))
        except sqlite3.DatabaseError:
            raise BusinessError("entitlement_conflict", "Unable to revoke entitlement.", 409) from None
