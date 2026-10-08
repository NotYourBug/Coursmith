"""Additive issuance snapshots and the approved delivery metadata contract.

Historical unknown snapshots and order lifecycle remain NULL. Legacy
conversion belongs to v005; this migration never invents product promises.
"""

import sqlite3


def _snapshot_check() -> str:
    # JSON CHECKs are portable to ordinary SQLite connections, including backup
    # and downstream services; no connection-local Python SQL functions needed.
    fields = ("course_id", "course_slug", "version", "package_hash", "title", "support_text")
    strings = " AND ".join(f"json_type(issued_policy_json, '$.{name}')='text'" for name in fields)
    no_credentials = " AND ".join(
        f"json_extract(issued_policy_json, '$.{name}') NOT GLOB '*{prefix}-[A-Za-z0-9_-]*'"
        for name in fields for prefix in ("CS", "LK"))
    return f"""issued_policy_json IS NULL OR CASE WHEN json_valid(issued_policy_json) THEN
        COALESCE(json_type(issued_policy_json)='object'
        AND json_type(issued_policy_json, '$.product_id')='integer'
        AND {strings}
        AND json_type(issued_policy_json, '$.access')='object'
        AND json_type(issued_policy_json, '$.access.access_mode')='text'
        AND json_extract(issued_policy_json, '$.access.update_policy')='current_version'
        AND json_type(issued_policy_json, '$.access.online') IN ('true','false')
        AND json_type(issued_policy_json, '$.access.pdf') IN ('true','false')
        AND json_type(issued_policy_json, '$.access.zip') IN ('true','false')
        AND ((json_extract(issued_policy_json, '$.access.access_mode')='days'
              AND json_type(issued_policy_json, '$.access.access_days')='integer'
              AND json_extract(issued_policy_json, '$.access.access_days') BETWEEN 1 AND 3650)
          OR (json_extract(issued_policy_json, '$.access.access_mode')='no_fixed_expiry'
              AND json_type(issued_policy_json, '$.access.access_days')='null'))
        AND {no_credentials}, 0)
        ELSE 0 END"""


def apply(connection: sqlite3.Connection) -> None:
    for table in ("code_batches", "access_codes", "entitlements", "orders"):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN issued_policy_json TEXT CHECK ({_snapshot_check()})")
        # SQLite CHECK cannot contain subqueries. Enforce strict extra='forbid'
        # with triggers after the structural CHECK has validated the JSON.
        for operation in ("INSERT", "UPDATE"):
            connection.execute(f"""CREATE TRIGGER {table}_snapshot_{operation.lower()}
                BEFORE {operation} ON {table} WHEN NEW.issued_policy_json IS NOT NULL
                AND CASE WHEN json_valid(NEW.issued_policy_json) THEN
                    (SELECT count(*) FROM json_each(NEW.issued_policy_json)) != 8
                    OR (SELECT count(*) FROM json_each(NEW.issued_policy_json, '$.access')) != 6
                ELSE 1 END
                BEGIN SELECT RAISE(ABORT, 'invalid issued policy snapshot'); END""")
    connection.execute("""ALTER TABLE code_batches ADD COLUMN revision INTEGER NOT NULL DEFAULT 1
        CHECK (typeof(revision)='integer' AND revision>=1)""")
    connection.execute("ALTER TABLE orders ADD COLUMN paid_at TEXT")
    connection.execute("""ALTER TABLE orders ADD COLUMN delivery_state TEXT
        CHECK (delivery_state IS NULL OR delivery_state IN ('recorded','code_ready','delivered','activated'))""")
    connection.execute("ALTER TABLE orders ADD COLUMN delivered_at TEXT")
    connection.execute("CREATE INDEX access_codes_batch_lookup ON access_codes(batch_id, id)")
    connection.execute("CREATE INDEX code_batches_page ON code_batches(id DESC)")
