"""Single-owner identity and transactional, hash-only admin sessions."""

from __future__ import annotations

import hashlib
import re
import secrets
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from pwdlib import PasswordHash

from ..audit import AuditEvent, append_event, record_denial
from ..database import LATEST_SCHEMA_VERSION, check_database, from_db_time, open_readonly, to_db_time, transaction
from ..domain import Actor, BusinessError, Clock, utc_now
from .security import RateLimiter


_PASSWORDS = PasswordHash.recommended()
_DUMMY_HASH = _PASSWORDS.hash(secrets.token_urlsafe(32))


def _digest(token: str) -> str:
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise BusinessError("invalid_session", "Please sign in again.", 401)
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _account(username: str) -> str:
    value = username.strip().casefold()
    if not value or len(value) > 128 or any(ord(char) < 32 for char in value):
        raise BusinessError("invalid_username", "Account must contain 1–128 characters.", 400)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise BusinessError("invalid_username", "Account contains invalid characters.", 400) from None
    return value


def _password(password: str) -> None:
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise BusinessError("invalid_password", "Password must contain 12–128 characters.", 400)
    try:
        password.encode("utf-8")
    except UnicodeError:
        raise BusinessError("invalid_password", "Password contains invalid characters.", 400) from None


def _matches(password: str, encoded: str) -> bool:
    try:
        _password(password)
    except BusinessError:
        return False
    return _PASSWORDS.verify(password, encoded)


@dataclass(frozen=True)
class AdminSessionGrant:
    token: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True)
class AdminSession:
    admin_id: int
    csrf_hash: str
    expires_at: datetime
    revision: int


class AdminService:
    def __init__(self, db_path: Path, *, clock: Clock = utc_now):
        self.db_path = Path(db_path)
        self.clock = clock
        self.rate_limiter = RateLimiter(self.db_path, clock=clock)

    def _require_schema(self) -> None:
        try:
            if check_database(self.db_path)["version"] != LATEST_SCHEMA_VERSION:
                raise BusinessError("admin_unavailable", "Run the approved database migration first.", 503)
        except BusinessError:
            raise BusinessError("admin_unavailable", "Run the approved database migration first.", 503) from None

    def is_initialized(self) -> bool:
        self._require_schema()
        with closing(open_readonly(self.db_path)) as connection:
            return connection.execute("SELECT 1 FROM admins WHERE role='owner'").fetchone() is not None

    def require_initialized(self) -> None:
        if not self.is_initialized():
            raise BusinessError("admin_uninitialized", "Initialize the owner with CLI init-admin.", 503)

    @staticmethod
    def _event(actor: Actor | None, action: str, *, error: BusinessError | None = None,
               changes: dict | None = None, request_id: str | None = None) -> AuditEvent:
        return AuditEvent(
            actor_admin_id=actor.admin_id if actor else None, object_type="admin",
            object_id=str(actor.admin_id) if actor else "owner", action=action,
            reason=error.code if error else "completed", outcome="denied" if error else "success",
            request_id=actor.request_id if actor else (request_id or secrets.token_hex(16)),
            changes={"error_code": error.code} if error else (changes or {}),
        )

    def _denial(self, actor: Actor | None, action: str, error: BusinessError,
                *, request_id: str | None = None) -> None:
        if actor:
            request_id = actor.request_id
            with closing(open_readonly(self.db_path)) as connection:
                known = connection.execute("SELECT 1 FROM admins WHERE id=?", (actor.admin_id,)).fetchone()
            if not known:
                actor = None
        record_denial(self.db_path, self._event(actor, action, error=error, request_id=request_id))
        error.denial_recorded = True

    def initialize_owner(self, username: str, password: str) -> int:
        self._require_schema()
        try:
            username = _account(username)
            _password(password)
            encoded = _PASSWORDS.hash(password)
            with transaction(self.db_path, immediate=True) as connection:
                if connection.execute("SELECT 1 FROM admins").fetchone():
                    raise BusinessError("owner_exists", "The owner has already been initialized.", 409)
                cursor = connection.execute(
                    "INSERT INTO admins (username, password_hash, role, created_at) VALUES (?, ?, 'owner', ?)",
                    (username, encoded, to_db_time(self.clock())),
                )
                admin_id = cursor.lastrowid
                append_event(connection, self._event(Actor(admin_id, secrets.token_hex(16)), "auth.initialize"))
                return admin_id
        except BusinessError as error:
            self._denial(None, "auth.initialize", error)
            raise

    def login(self, username: str, password: str, *, source: str, request_id: str,
              previous_token: str | None = None) -> AdminSessionGrant:
        self.require_initialized()
        # Account bucket normalization is shared by every HTTP/CLI login.
        account = username.strip().casefold()
        try:
            with transaction(self.db_path, immediate=True) as connection:
                self.rate_limiter.check_login_in_tx(connection, account, source)
                connection.execute("SAVEPOINT login_verification")
                owner = connection.execute("SELECT * FROM admins WHERE username=? AND role='owner'", (account,)).fetchone()
                valid = _matches(password, owner["password_hash"] if owner else _DUMMY_HASH)
                if owner is None or not valid or not owner["enabled"]:
                    # Roll back rejected authentication, retaining the writer
                    # lock while committing only its security counters.
                    connection.execute("ROLLBACK TO login_verification")
                    connection.execute("RELEASE login_verification")
                    self.rate_limiter.record_login_failure_in_tx(connection, account, source)
                else:
                    connection.execute("RELEASE login_verification")
                    now = self.clock()
                    expires_at = now + timedelta(hours=8)
                    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                    if previous_token:
                        try:
                            previous_hash = _digest(previous_token)
                        except BusinessError:
                            previous_hash = None
                        if previous_hash:
                            connection.execute(
                                "UPDATE admin_sessions SET revoked_at=? WHERE token_hash=? AND admin_id=? AND revoked_at IS NULL",
                                (to_db_time(now), previous_hash, owner["id"]),
                            )
                    connection.execute(
                        """INSERT INTO admin_sessions
                           (token_hash, admin_id, csrf_hash, created_at, last_activity_at, expires_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (_digest(token), owner["id"], _digest(csrf), to_db_time(now), to_db_time(now), to_db_time(expires_at)),
                    )
                    append_event(connection, self._event(Actor(owner["id"], request_id), "auth.login"))
                    return AdminSessionGrant(token, csrf, expires_at)
            raise BusinessError("invalid_credentials", "Account or password is incorrect.", 401)
        except BusinessError as error:
            self._denial(None, "auth.login", error, request_id=request_id)
            raise

    def _live_session(self, connection, token_hash: str):
        row = connection.execute(
            """SELECT s.*, a.enabled, a.role, a.revision FROM admin_sessions s
               JOIN admins a ON a.id=s.admin_id WHERE s.token_hash=?""", (token_hash,),
        ).fetchone()
        now = self.clock()
        if (row is None or not row["enabled"] or row["role"] != "owner" or row["revoked_at"] is not None
                or from_db_time(row["expires_at"]) <= now
                or from_db_time(row["last_activity_at"]) + timedelta(minutes=30) <= now):
            raise BusinessError("invalid_session", "Please sign in again.", 401)
        return row

    def require_session(self, raw_token: str, *, request_id: str) -> AdminSession:
        self.require_initialized()
        token_hash = _digest(raw_token)
        with transaction(self.db_path, immediate=True) as connection:
            row = self._live_session(connection, token_hash)
            connection.execute("UPDATE admin_sessions SET last_activity_at=? WHERE token_hash=?",
                               (to_db_time(self.clock()), token_hash))
            return AdminSession(row["admin_id"], row["csrf_hash"], from_db_time(row["expires_at"]), row["revision"])

    def logout(self, raw_token: str, actor: Actor) -> None:
        self.require_initialized()
        try:
            with transaction(self.db_path, immediate=True) as connection:
                token_hash = _digest(raw_token)
                row = self._live_session(connection, token_hash)
                if row["admin_id"] != actor.admin_id:
                    raise BusinessError("invalid_session", "Please sign in again.", 401)
                connection.execute("UPDATE admin_sessions SET revoked_at=? WHERE token_hash=?",
                                   (to_db_time(self.clock()), token_hash))
                append_event(connection, self._event(actor, "auth.logout"))
        except BusinessError as error:
            self._denial(actor, "auth.logout", error)
            raise

    def change_password(self, actor: Actor, current_password: str, new_password: str, *,
                        expected_revision: int | None = None) -> None:
        self.require_initialized()
        try:
            _password(new_password)
            with transaction(self.db_path, immediate=True) as connection:
                owner = connection.execute("SELECT * FROM admins WHERE id=? AND role='owner' AND enabled=1",
                                           (actor.admin_id,)).fetchone()
                if owner is None:
                    raise BusinessError("invalid_session", "Please sign in again.", 401)
                if expected_revision is not None and owner["revision"] != expected_revision:
                    raise BusinessError("stale_revision", "This form is stale; reload and try again.", 409)
                if not _matches(current_password, owner["password_hash"]):
                    raise BusinessError("invalid_credentials", "Current password is incorrect.", 401)
                connection.execute("UPDATE admins SET password_hash=?, revision=revision+1 WHERE id=?",
                                   (_PASSWORDS.hash(new_password), actor.admin_id))
                revoked = connection.execute(
                    "UPDATE admin_sessions SET revoked_at=? WHERE admin_id=? AND revoked_at IS NULL",
                    (to_db_time(self.clock()), actor.admin_id),
                ).rowcount
                append_event(connection, self._event(actor, "admin.password_change", changes={
                    "revoked_sessions": revoked, "revision": owner["revision"] + 1,
                }))
        except BusinessError as error:
            self._denial(actor, "admin.password_change", error)
            raise
