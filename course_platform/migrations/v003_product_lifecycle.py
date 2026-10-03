"""Allow unbound archival and honestly attributed automatic product imports.

v001/v002 remain immutable. The runner supplies one backed-up transaction
with foreign-key enforcement temporarily disabled on its private connection.
"""

import sqlite3

from ..database import validate_database
from ..domain import BusinessError


def validate_graph(connection: sqlite3.Connection) -> None:
    """Check both declarative FKs and v002's legacy-table trigger links."""
    validate_database(connection)
    invalid_codes = connection.execute("""SELECT 1 FROM access_codes AS code WHERE
        (code.product_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM products WHERE id=code.product_id AND course_id=code.course_id))
        OR (code.batch_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM code_batches WHERE id=code.batch_id AND product_id=code.product_id))
        OR (code.order_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM orders WHERE id=code.order_id AND product_id=code.product_id)) LIMIT 1""").fetchone()
    invalid_sessions = connection.execute("""SELECT 1 FROM sessions AS session WHERE
        (session.entitlement_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM entitlements WHERE id=session.entitlement_id AND course_id=session.course_id))
        OR (session.source_code_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM access_codes WHERE id=session.source_code_id AND course_id=session.course_id)) LIMIT 1""").fetchone()
    if invalid_codes or invalid_sessions:
        raise BusinessError("database_foreign_keys", "Database course/product links are inconsistent.", 409)


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def apply(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
        raise BusinessError("migration_transaction", "Product rebuild requires the migration runner transaction.", 409)
    validate_graph(connection)
    # Triggers/views on other tables can reference products too. Preserve their
    # definitions while avoiding an invalid intermediate schema during rename.
    dependents = connection.execute("""SELECT type, name, sql FROM sqlite_master
        WHERE type IN ('trigger', 'view') ORDER BY type, name""").fetchall()
    indexes = connection.execute("""SELECT sql FROM sqlite_master
        WHERE type='index' AND tbl_name='products' AND sql IS NOT NULL ORDER BY name""").fetchall()
    for kind, name, _ in dependents:
        connection.execute(f"DROP {kind.upper()} {_quoted(name)}")
    connection.execute("""CREATE TABLE products_lifecycle (
        id INTEGER PRIMARY KEY,
        course_id TEXT UNIQUE REFERENCES courses(course_id),
        title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        category_id INTEGER REFERENCES categories(id),
        status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'paused', 'archived')),
        access_days INTEGER CHECK (access_days IS NULL OR (typeof(access_days) = 'integer' AND access_days BETWEEN 1 AND 3650)),
        update_policy TEXT CHECK (update_policy IS NULL OR update_policy = 'current_version'),
        sales_check_json TEXT,
        sales_checked_at TEXT,
        sales_package_hash TEXT,
        revision INTEGER NOT NULL DEFAULT 1 CHECK (typeof(revision) = 'integer' AND revision >= 1),
        created_by INTEGER REFERENCES admins(id),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')),
        updated_at TEXT,
        UNIQUE (id, course_id),
        CHECK (status IN ('draft', 'archived') OR course_id IS NOT NULL)
    )""")
    columns = """id, course_id, title, description, category_id, status, access_days, update_policy,
        sales_check_json, sales_checked_at, sales_package_hash, revision, created_by, created_at, updated_at"""
    connection.execute(f"INSERT INTO products_lifecycle ({columns}) SELECT {columns} FROM products")
    connection.execute("DROP TABLE products")
    connection.execute("ALTER TABLE products_lifecycle RENAME TO products")
    for (sql,) in indexes:
        connection.execute(sql)
    # Views must exist before restoring their INSTEAD OF triggers.
    for _, _, sql in reversed(dependents):
        connection.execute(sql)
    validate_graph(connection)
