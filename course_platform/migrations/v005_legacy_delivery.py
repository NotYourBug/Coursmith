"""Preserve individual historical rights, without inventing ownership/policy."""

import sqlite3
from pathlib import Path

from ..content_inspection import inspect_package
from ..domain import BusinessError
from ..operations.products import ProductService


def apply(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE legacy_code_origins (
        code_id INTEGER PRIMARY KEY REFERENCES access_codes(id),
        code_hash TEXT NOT NULL,
        original_expires_at TEXT,
        original_created_at TEXT NOT NULL,
        original_used_at TEXT,
        course_version TEXT NOT NULL,
        package_hash TEXT,
        content_available INTEGER NOT NULL CHECK (content_available IN (0,1))
    )""")
    connection.execute("""CREATE TABLE legacy_entitlement_origins (
        entitlement_id INTEGER PRIMARY KEY REFERENCES entitlements(id),
        session_hash TEXT NOT NULL UNIQUE REFERENCES sessions(session_hash),
        original_expires_at TEXT,
        original_created_at TEXT NOT NULL,
        course_version TEXT NOT NULL,
        package_hash TEXT,
        content_available INTEGER NOT NULL CHECK (content_available IN (0,1))
    )""")
    connection.execute("""CREATE TABLE legacy_verifications (
        id INTEGER PRIMARY KEY,
        code_id INTEGER UNIQUE REFERENCES legacy_code_origins(code_id),
        entitlement_id INTEGER UNIQUE REFERENCES entitlements(id),
        actor_admin_id INTEGER NOT NULL REFERENCES admins(id),
        audit_event_id INTEGER NOT NULL UNIQUE REFERENCES admin_events(id),
        purpose TEXT NOT NULL CHECK (purpose IN ('sale','test','gift')),
        order_id INTEGER REFERENCES orders(id),
        issued_policy_json TEXT,
        original_expires_at TEXT,
        expires_at TEXT,
        reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 1000),
        verified_at TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (typeof(revision)='integer' AND revision>=2),
        CHECK ((code_id IS NULL) != (entitlement_id IS NULL))
    )""")
    products = ProductService(Path("."))  # ensure_draft_in_tx uses only caller's connection.
    for course in connection.execute("SELECT * FROM courses ORDER BY course_id").fetchall():
        existing = connection.execute("SELECT id FROM products WHERE course_id=?", (course["course_id"],)).fetchone()
        product_id = existing["id"] if existing else products.ensure_draft_in_tx(connection, course["course_id"]).id
        fingerprint, available = course["package_hash"], False
        try:
            inspected = inspect_package(Path(course["content_path"]))
            available = ((inspected.course_id, inspected.slug, inspected.version) ==
                (course["course_id"], course["slug"], course["version"]) and
                (fingerprint is None or fingerprint == inspected.fingerprint))
            if available and fingerprint is None:
                fingerprint = inspected.fingerprint
                connection.execute("UPDATE courses SET package_hash=? WHERE course_id=? AND package_hash IS NULL",
                    (fingerprint, course["course_id"]))
        except BusinessError:
            pass
        for code in connection.execute("""SELECT * FROM access_codes WHERE course_id=?
            AND batch_id IS NULL AND created_by IS NULL AND issued_policy_json IS NULL
            AND legacy_state IS NULL""", (course["course_id"],)).fetchall():
            connection.execute("INSERT INTO legacy_code_origins VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (code["id"], code["code_hash"], code["expires_at"], code["created_at"], code["used_at"],
                 course["version"], fingerprint, int(available)))
            connection.execute("""UPDATE access_codes SET legacy_state='pending_verification',
                product_id=coalesce(product_id, ?) WHERE id=?""", (product_id, code["id"]))
        for session in connection.execute("SELECT * FROM sessions WHERE course_id=? AND entitlement_id IS NULL",
            (course["course_id"],)).fetchall():
            # No code/session association is inferred from course or timestamps.
            entitlement_id = connection.execute("""INSERT INTO entitlements
                (course_id, product_id, course_version, package_hash, expires_at, legacy_state, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending_verification', ?)""",
                (course["course_id"], product_id, course["version"], fingerprint,
                 session["expires_at"], session["created_at"])).lastrowid
            connection.execute("UPDATE sessions SET entitlement_id=? WHERE session_hash=?",
                (entitlement_id, session["session_hash"]))
            connection.execute("INSERT INTO legacy_entitlement_origins VALUES (?, ?, ?, ?, ?, ?, ?)",
                (entitlement_id, session["session_hash"], session["expires_at"], session["created_at"],
                 course["version"], fingerprint, int(available)))
            connection.execute("""INSERT INTO entitlement_progress
                (entitlement_id, course_id, chapter_number, completed, updated_at)
                SELECT ?, course_id, chapter_number, completed, updated_at FROM progress
                WHERE session_hash=? AND course_id=?""", (entitlement_id, session["session_hash"], course["course_id"]))
