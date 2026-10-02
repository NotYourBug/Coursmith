"""SQLite persistence for course catalog, access, progress, and audit events."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .content import CourseManifest


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS courses (
    course_id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL,
    content_path TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chapters (
    course_id TEXT NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
    chapter_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    path TEXT NOT NULL,
    free_preview INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (course_id, chapter_number)
);

CREATE TABLE IF NOT EXISTS access_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id TEXT NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    used_at TEXT,
    UNIQUE (course_id, code_hash)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_hash TEXT PRIMARY KEY,
    course_id TEXT NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS progress (
    session_hash TEXT NOT NULL REFERENCES sessions(session_hash) ON DELETE CASCADE,
    course_id TEXT NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
    chapter_number INTEGER NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_hash, course_id, chapter_number),
    FOREIGN KEY (course_id, chapter_number)
        REFERENCES chapters(course_id, chapter_number) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    course_id TEXT,
    session_hash TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_db_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def from_db_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(path: Path) -> None:
    with connect(path) as connection:
        connection.executescript(SCHEMA)


def sync_course(manifest: CourseManifest, content_path: Path, database_path: Path) -> None:
    """Upsert catalog metadata while keeping lesson HTML on disk."""
    initialize_database(database_path)
    now = to_db_time(utc_now())
    with connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO courses
                (course_id, slug, title, category, version, status, content_path, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(course_id) DO UPDATE SET
                slug=excluded.slug, title=excluded.title, category=excluded.category,
                version=excluded.version, status=excluded.status,
                content_path=excluded.content_path, updated_at=excluded.updated_at
            """,
            (
                manifest.course_id,
                manifest.slug,
                manifest.title,
                manifest.category,
                manifest.version,
                manifest.status,
                str(Path(content_path).resolve()),
                now,
            ),
        )
        connection.executemany(
            """
            INSERT INTO chapters (course_id, chapter_number, title, path, free_preview)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(course_id, chapter_number) DO UPDATE SET
                title=excluded.title,
                path=excluded.path,
                free_preview=excluded.free_preview
            """,
            [
                (
                    manifest.course_id,
                    chapter.number,
                    chapter.title,
                    chapter.path,
                    int(chapter.free_preview),
                )
                for chapter in manifest.chapters
            ],
        )
        chapter_numbers = [chapter.number for chapter in manifest.chapters]
        placeholders = ", ".join("?" for _ in chapter_numbers)
        connection.execute(
            f"""
            DELETE FROM chapters
            WHERE course_id = ? AND chapter_number NOT IN ({placeholders})
            """,
            (manifest.course_id, *chapter_numbers),
        )
