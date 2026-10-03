"""Persistent independent login buckets and the shared public credential bucket."""

from __future__ import annotations

import hashlib
import math
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from ..audit import AuditEvent, record_denial
from ..database import from_db_time, to_db_time, transaction
from ..domain import BusinessError, Clock, utc_now


class _RateLimitError(BusinessError):
    def __init__(self, until: datetime, now: datetime):
        super().__init__("rate_limited", "Too many attempts; try again later.", 429)
        self.retry_after = max(1, math.ceil((until - now).total_seconds()))
        self.headers = {"Retry-After": str(self.retry_after)}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _row(connection: sqlite3.Connection, key: tuple[str, str, str]) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM request_limits WHERE scope=? AND source_digest=? AND account_key=?", key,
    ).fetchone()


def _write(connection: sqlite3.Connection, key: tuple[str, str, str], started: datetime,
           count: int, blocked: datetime | None = None) -> None:
    connection.execute(
        """INSERT INTO request_limits
           (scope, source_digest, account_key, window_started_at, count, blocked_until)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(scope, source_digest, account_key) DO UPDATE SET
           window_started_at=excluded.window_started_at, count=excluded.count,
           blocked_until=excluded.blocked_until""",
        (*key, to_db_time(started), count, to_db_time(blocked) if blocked else None),
    )


class RateLimiter:
    """Use one writer transaction per counter operation; never auto-migrate.

    Account keys must be normalized by the authentication domain. Checks do
    not count successful logins; only record_login_failure increments both
    independent buckets. Public redemption/recovery callers share check_public.
    429 errors carry headers['Retry-After'] for the future HTTP error handler.
    """

    def __init__(self, db_path: Path, *, clock: Clock = utc_now):
        self.db_path = db_path
        self.clock = clock

    @staticmethod
    def _login_keys(account_key: str, source_key: str) -> tuple[tuple[str, str, str], ...]:
        return (("login_account", "", _digest(account_key)), ("login_source", _digest(source_key), ""))

    def _denial(self, bucket: str) -> None:
        # Only fixed metadata and a server-generated request ID enter the audit.
        # Called after the counter transaction has exited, never under its lock.
        record_denial(self.db_path, AuditEvent(
            actor_admin_id=None, object_type="request", object_id=bucket,
            action="security.rate_limit", reason="rate_limited", outcome="denied",
            request_id=secrets.token_hex(16), changes={"error_code": "rate_limited"},
        ))

    def check_login(self, account_key: str, source_key: str) -> None:
        try:
            with transaction(self.db_path, immediate=True) as connection:
                self.check_login_in_tx(connection, account_key, source_key)
        except _RateLimitError:
            self._denial("login")
            raise

    def check_login_in_tx(self, connection: sqlite3.Connection, account_key: str, source_key: str) -> None:
        """Caller holds the writer lock through verification and failure updates.

        The owning service records any denial after its transaction exits.
        """
        now = self.clock()
        blocked = []
        for key in self._login_keys(account_key, source_key):
            row = _row(connection, key)
            if row is not None and row["blocked_until"] is not None:
                until = from_db_time(row["blocked_until"])
                if until > now:
                    blocked.append(until)
        if blocked:
            raise _RateLimitError(max(blocked), now)

    def record_login_failure(self, account_key: str, source_key: str) -> None:
        with transaction(self.db_path, immediate=True) as connection:
            self.record_login_failure_in_tx(connection, account_key, source_key)

    def record_login_failure_in_tx(self, connection: sqlite3.Connection, account_key: str, source_key: str) -> None:
        """Persist both counters on the owner's existing writer connection."""
        now = self.clock()
        for key in self._login_keys(account_key, source_key):
            row = _row(connection, key)
            started, count = now, 1
            if row is not None:
                blocked = from_db_time(row["blocked_until"]) if row["blocked_until"] else None
                if blocked is not None and blocked > now:
                    continue  # Rejected retries must not prolong a lockout.
                previous = from_db_time(row["window_started_at"])
                if blocked is None and previous + timedelta(minutes=15) > now:
                    started, count = previous, row["count"] + 1
            _write(connection, key, started, count, now + timedelta(minutes=15) if count >= 5 else None)

    def check_public(self, source_key: str) -> None:
        try:
            with transaction(self.db_path, immediate=True) as connection:
                now = self.clock()
                key = ("public_credentials", _digest(source_key), "")
                row = _row(connection, key)
                started, count = now, 1
                if row is not None:
                    previous = from_db_time(row["window_started_at"])
                    until = previous + timedelta(minutes=1)
                    if until > now:
                        if row["count"] >= 10:
                            raise _RateLimitError(until, now)
                        started, count = previous, row["count"] + 1
                _write(connection, key, started, count)
        except _RateLimitError:
            self._denial("public_credentials")
            raise
