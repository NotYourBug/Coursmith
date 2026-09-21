"""Redeemable course access and progress service."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .database import connect, from_db_time, to_db_time, utc_now


class InvalidAccessCode(Exception):
    """Raised when a code is invalid, expired, consumed, or mismatched."""


class InvalidSession(Exception):
    """Raised when a session is missing, expired, or used for another course."""


@dataclass(frozen=True)
class AccessSession:
    session_id: str
    course_id: str
    expires_at: datetime


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AccessService:
    def __init__(self, database_path: Path, session_ttl_hours: int = 72):
        if session_ttl_hours <= 0:
            raise ValueError("session_ttl_hours must be greater than zero")
        self.database_path = Path(database_path)
        self.session_ttl_hours = session_ttl_hours

    def create_access_code(
        self, course_id: str, expires_at: datetime | None = None
    ) -> str:
        raw_code = "CS-" + secrets.token_urlsafe(12)
        now = utc_now()
        with connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO access_codes (course_id, code_hash, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    course_id,
                    _hash(raw_code),
                    to_db_time(now),
                    to_db_time(expires_at) if expires_at else None,
                ),
            )
        return raw_code

    def redeem_access_code(self, raw_code: str, course_id: str) -> AccessSession:
        now = utc_now()
        code_hash = _hash(raw_code)
        session_id = secrets.token_urlsafe(32)
        session_hash = _hash(session_id)
        expires_at = now + timedelta(hours=self.session_ttl_hours)
        with connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id, course_id, expires_at, used_at
                FROM access_codes WHERE code_hash = ? AND course_id = ?
                """,
                (code_hash, course_id),
            ).fetchone()
            if row is None:
                self._audit(connection, "access_denied", course_id, None, {"reason": "invalid_code"})
                raise InvalidAccessCode("兑换码无效或不属于该课程")
            if row["used_at"] is not None:
                self._audit(connection, "access_denied", course_id, None, {"reason": "used_code"})
                raise InvalidAccessCode("兑换码已经使用")
            if row["expires_at"] and from_db_time(row["expires_at"]) <= now:
                self._audit(connection, "access_denied", course_id, None, {"reason": "expired_code"})
                raise InvalidAccessCode("兑换码已过期")
            course = connection.execute(
                "SELECT course_id FROM courses WHERE course_id = ?", (course_id,)
            ).fetchone()
            if course is None:
                raise InvalidAccessCode("课程不存在")
            connection.execute(
                "UPDATE access_codes SET used_at = ? WHERE id = ? AND used_at IS NULL",
                (to_db_time(now), row["id"]),
            )
            connection.execute(
                "INSERT INTO sessions (session_hash, course_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (session_hash, course_id, to_db_time(now), to_db_time(expires_at)),
            )
            self._audit(connection, "code_redeemed", course_id, session_hash, {})
        return AccessSession(session_id, course_id, expires_at)

    def get_session(self, raw_session_id: str) -> AccessSession | None:
        if not raw_session_id:
            return None
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT course_id, expires_at FROM sessions WHERE session_hash = ?",
                (_hash(raw_session_id),),
            ).fetchone()
        if row is None or from_db_time(row["expires_at"]) <= utc_now():
            return None
        return AccessSession(raw_session_id, row["course_id"], from_db_time(row["expires_at"]))

    def require_session(self, raw_session_id: str, course_id: str) -> AccessSession:
        session = self.get_session(raw_session_id)
        if session is None or session.course_id != course_id:
            raise InvalidSession("访问会话无效或已过期")
        return session

    def record_progress(
        self, session_id: str, course_id: str, chapter_number: int, completed: bool
    ) -> None:
        session = self.require_session(session_id, course_id)
        now = to_db_time(utc_now())
        with connect(self.database_path) as connection:
            chapter = connection.execute(
                "SELECT 1 FROM chapters WHERE course_id = ? AND chapter_number = ?",
                (course_id, chapter_number),
            ).fetchone()
            if chapter is None:
                raise ValueError("chapter does not exist")
            connection.execute(
                """
                INSERT INTO progress
                    (session_hash, course_id, chapter_number, completed, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_hash, course_id, chapter_number) DO UPDATE SET
                    completed=excluded.completed, updated_at=excluded.updated_at
                """,
                (_hash(session.session_id), course_id, chapter_number, int(completed), now),
            )
            self._audit(
                connection,
                "progress_updated",
                course_id,
                _hash(session.session_id),
                {"chapter_number": chapter_number, "completed": bool(completed)},
            )

    def get_progress(self, session_id: str, course_id: str) -> dict[int, bool]:
        session = self.require_session(session_id, course_id)
        with connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT chapter_number, completed FROM progress WHERE session_hash = ? AND course_id = ?",
                (_hash(session.session_id), course_id),
            ).fetchall()
        return {int(row["chapter_number"]): bool(row["completed"]) for row in rows}

    @staticmethod
    def _audit(connection, event_type, course_id, session_hash, metadata):
        connection.execute(
            "INSERT INTO events (event_type, course_id, session_hash, metadata_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (event_type, course_id, session_hash, json.dumps(metadata, ensure_ascii=False), to_db_time(utc_now())),
        )
