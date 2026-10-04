"""SQLite persistence for course catalog, access, progress, and audit events."""

from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from .content import CourseManifest
from .domain import BusinessError, utc_now


# Immutable historical six-table baseline consumed by v001. Never extend or
# change this SQL; all subsequent schema changes belong to new migrations.
SCHEMA: Final[str] = """
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
    connection = sqlite3.connect(path, timeout=3)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 3000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def transaction(path: Path, *, immediate: bool = False):
    """Own one connection and transaction, including rollback and closure.

    Cross-domain writes must pass this connection to their in-transaction
    functions. A denial is recorded separately after this context exits.
    """
    connection = connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield connection
        connection.commit()
    except BaseException as exc:
        connection.rollback()
        if isinstance(exc, sqlite3.OperationalError) and (
            getattr(exc, "sqlite_errorcode", 0) & 0xFF
        ) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise BusinessError("database_busy", "Database is busy; retry the operation.", 503) from None
        raise
    finally:
        connection.close()


def open_readonly(path: Path) -> sqlite3.Connection:
    """Open an existing database without creating a file or changing pragmas."""
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 3000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def validate_database(connection: sqlite3.Connection) -> None:
    if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
        raise BusinessError("database_integrity", "Database integrity check failed.", 409)
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise BusinessError("database_foreign_keys", "Database foreign_key check failed.", 409)


def backup_database(source: Path, target: Path) -> None:
    """Reserve a new target, copy with SQLite's backup API, then validate it."""
    source, target = Path(source), Path(target)
    if not source.is_file():
        raise BusinessError("database_missing", "Source database is missing.", 404)
    if source.resolve() == target.resolve():
        raise BusinessError("backup_target", "Backup must use a different, unused path.", 409)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Exclusive creation protects existing files even when another process
        # races the initial existence check. Never remove an unreserved target.
        with target.open("xb"):
            pass
    except FileExistsError:
        raise BusinessError("backup_exists", "Backup target already exists.", 409) from None
    try:
        with closing(open_readonly(source)) as original, closing(connect(target)) as backup:
            original.backup(backup)
            validate_database(backup)
    except BaseException:
        target.unlink()
        raise


LATEST_SCHEMA_VERSION = 5


@contextmanager
def _product_rebuild_transaction(path: Path):
    """Only the v003 runner may suspend FKs, on its own short-lived connection.

    SQLite requires this pragma before BEGIN. The whole upgrade and graph
    validation stay atomic; normal application transactions retain FK=ON.
    """
    with closing(connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException as exc:
            connection.rollback()
            if isinstance(exc, sqlite3.OperationalError) and (
                getattr(exc, "sqlite_errorcode", 0) & 0xFF
            ) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise BusinessError("database_busy", "Database is busy; retry the operation.", 503) from None
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")


@dataclass(frozen=True)
class MigrationReport:
    from_version: int
    to_version: int
    backup_path: Path | None


def _version(connection: sqlite3.Connection) -> int:
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone():
        return 0
    versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
    if versions != list(range(1, len(versions) + 1)) or len(versions) > LATEST_SCHEMA_VERSION:
        raise BusinessError("migration_version", "Unknown or incomplete migration history.", 409)
    return len(versions)


def check_database(path: Path) -> dict[str, object]:
    """Read-only inspection for the CLI; includes integrity and foreign keys."""
    if not Path(path).is_file():
        raise BusinessError("database_missing", "Database is missing.", 404)
    try:
        with closing(open_readonly(path)) as connection:
            version = _version(connection)
            validate_database(connection)
            return {"version": version, "integrity": "ok", "foreign_keys": "ok"}
    except sqlite3.DatabaseError:
        raise BusinessError("database_invalid", "Database could not be validated.", 409) from None


def migrate_database(
    path: Path, *, backup_path: Path | None = None, through_version: int | None = None
) -> MigrationReport:
    from .migrations import MIGRATIONS, v003_product_lifecycle

    migrations = MIGRATIONS
    path = Path(path)
    target = LATEST_SCHEMA_VERSION if through_version is None else through_version
    if type(target) is not int or not 1 <= target <= LATEST_SCHEMA_VERSION:
        raise BusinessError("migration_target", "Unsupported migration target.", 400)
    existing = path.exists()
    current = int(check_database(path)["version"]) if existing else 0
    if target < current:
        raise BusinessError("migration_downgrade", "Database downgrades are not supported.", 409)
    if target == current:
        return MigrationReport(current, current, None)
    if existing and backup_path is None:
        raise BusinessError("backup_required", "An unused backup path is required before upgrading.", 409)
    migration_transaction = (_product_rebuild_transaction(path) if current < 3 <= target
                             else transaction(path, immediate=True))
    with migration_transaction as connection:
        # Acquire the writer boundary before backup. A separate read connection
        # can copy the committed source while this lock prevents concurrent writes.
        locked_version = _version(connection)
        if locked_version != current:
            raise BusinessError("migration_changed", "Migration history changed; retry inspection.", 409)
        actual_backup = Path(backup_path) if existing and backup_path is not None else None
        if actual_backup is not None:
            backup_database(path, actual_backup)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(current + 1, target + 1):
            migrations[version - 1].apply(connection)
            connection.execute("INSERT INTO schema_migrations VALUES (?, ?)", (version, to_db_time(utc_now())))
        if target >= 3:
            v003_product_lifecycle.validate_graph(connection)
        else:
            validate_database(connection)
    return MigrationReport(current, target, actual_backup)


def initialize_database(path: Path) -> None:
    # Legacy startup remains the six-table compatibility baseline. Upgrades
    # are explicit via migrate_database/CLI and must never happen on startup.
    from .migrations.v001_baseline import apply

    with transaction(path) as connection:
        apply(connection)


def sync_course(manifest: CourseManifest, content_path: Path, database_path: Path) -> None:
    """Import one release; identical repeats never overwrite its baseline."""
    from .content_inspection import inspect_package, read_verified_file
    root = Path(content_path).resolve()
    inspection = inspect_package(root)
    actual = CourseManifest.model_validate_json(read_verified_file(root, "manifest.json", inspection.fingerprint))
    if actual != manifest:
        raise BusinessError("manifest_mismatch", "Provided manifest does not match the release.", 409)
    # Historical callers still build the unchanged six-table source database.
    # Production startup initializes latest schema explicitly before this call.
    initialize_database(database_path)
    with transaction(database_path, immediate=True) as connection:
        existing = connection.execute("SELECT * FROM courses WHERE course_id=?", (manifest.course_id,)).fetchone()
        values = (manifest.course_id, manifest.slug, manifest.title, manifest.category,
                  manifest.version, manifest.status, str(root))
        chapters = [(c.number, c.title, c.path, int(c.free_preview)) for c in manifest.chapters]
        if existing:
            saved = tuple(existing[k] for k in ("course_id", "slug", "title", "category", "version", "status", "content_path"))
            stored = [tuple(row) for row in connection.execute(
                "SELECT chapter_number, title, path, free_preview FROM chapters WHERE course_id=? ORDER BY chapter_number",
                (manifest.course_id,))]
            if saved != values or stored != chapters or (
                "package_hash" in existing.keys() and existing["package_hash"] != inspection.fingerprint):
                raise BusinessError("release_exists", "M1 forbids changing an existing release; retain its original content.", 409)
            return
        connection.execute("""INSERT INTO courses
            (course_id, slug, title, category, version, status, content_path, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (*values, to_db_time(utc_now())))
        connection.executemany("""INSERT INTO chapters
            (course_id, chapter_number, title, path, free_preview) VALUES (?, ?, ?, ?, ?)""",
            [(manifest.course_id, *chapter) for chapter in chapters])
        if "package_hash" in [row[1] for row in connection.execute("PRAGMA table_info(courses)")]:
            connection.execute("UPDATE courses SET package_hash=? WHERE course_id=?",
                               (inspection.fingerprint, manifest.course_id))
