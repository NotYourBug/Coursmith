"""Owner operations schema only; legacy data conversion belongs to v3."""

import sqlite3


def apply(connection: sqlite3.Connection) -> None:
    statements = (
        """CREATE TABLE admins (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'owner' UNIQUE CHECK (role = 'owner'),
            enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (typeof(revision) = 'integer' AND revision >= 1),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'))
        )""",
        """CREATE TABLE admin_sessions (
            token_hash TEXT PRIMARY KEY NOT NULL,
            admin_id INTEGER NOT NULL REFERENCES admins(id),
            csrf_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_activity_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT
        )""",
        """CREATE TABLE request_limits (
            scope TEXT NOT NULL,
            source_digest TEXT NOT NULL,
            account_key TEXT NOT NULL DEFAULT '',
            window_started_at TEXT NOT NULL,
            count INTEGER NOT NULL CHECK (typeof(count) = 'integer' AND count >= 0),
            blocked_until TEXT,
            PRIMARY KEY (scope, source_digest, account_key)
        )""",
        """CREATE TABLE categories (
            id INTEGER PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0 CHECK (typeof(sort_order) = 'integer'),
            enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (typeof(revision) = 'integer' AND revision >= 1),
            created_by INTEGER REFERENCES admins(id)
        )""",
        """CREATE TABLE products (
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
            created_by INTEGER NOT NULL REFERENCES admins(id),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')),
            updated_at TEXT,
            UNIQUE (id, course_id),
            CHECK (status = 'draft' OR course_id IS NOT NULL)
        )""",
        """CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            product_id INTEGER NOT NULL REFERENCES products(id),
            channel TEXT NOT NULL,
            shop TEXT NOT NULL,
            external_order_id TEXT NOT NULL,
            amount_cents INTEGER NOT NULL CHECK (typeof(amount_cents) = 'integer' AND amount_cents >= 0),
            status TEXT NOT NULL DEFAULT 'paid' CHECK (status IN ('paid', 'refunded')),
            notes TEXT NOT NULL DEFAULT '' CHECK (length(notes) <= 1000),
            refunded_at TEXT,
            refund_reason TEXT,
            revision INTEGER NOT NULL DEFAULT 1 CHECK (typeof(revision) = 'integer' AND revision >= 1),
            created_by INTEGER NOT NULL REFERENCES admins(id),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')),
            UNIQUE (channel, shop, external_order_id),
            UNIQUE (id, product_id)
        )""",
        """CREATE TABLE code_batches (
            id INTEGER PRIMARY KEY,
            product_id INTEGER NOT NULL REFERENCES products(id),
            quantity INTEGER NOT NULL DEFAULT 1 CHECK (typeof(quantity) = 'integer' AND quantity BETWEEN 1 AND 200),
            purpose TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            access_days INTEGER CHECK (access_days IS NULL OR (typeof(access_days) = 'integer' AND access_days BETWEEN 1 AND 3650)),
            activation_days INTEGER NOT NULL DEFAULT 30 CHECK (typeof(activation_days) = 'integer' AND activation_days BETWEEN 1 AND 365),
            update_policy TEXT NOT NULL DEFAULT 'current_version' CHECK (update_policy = 'current_version'),
            course_version TEXT,
            package_hash TEXT,
            notes TEXT NOT NULL DEFAULT '' CHECK (length(notes) <= 1000),
            created_by INTEGER NOT NULL REFERENCES admins(id),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')),
            UNIQUE (created_by, idempotency_key),
            UNIQUE (id, product_id)
        )""",
        "ALTER TABLE courses ADD COLUMN package_hash TEXT",
        "ALTER TABLE courses ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK (typeof(revision) = 'integer' AND revision >= 1)",
        "ALTER TABLE access_codes ADD COLUMN batch_id INTEGER REFERENCES code_batches(id)",
        "ALTER TABLE access_codes ADD COLUMN product_id INTEGER REFERENCES products(id)",
        "ALTER TABLE access_codes ADD COLUMN order_id INTEGER REFERENCES orders(id)",
        "ALTER TABLE access_codes ADD COLUMN code_number TEXT",
        "ALTER TABLE access_codes ADD COLUMN access_days INTEGER CHECK (access_days IS NULL OR (typeof(access_days) = 'integer' AND access_days BETWEEN 1 AND 3650))",
        "ALTER TABLE access_codes ADD COLUMN update_policy TEXT CHECK (update_policy IS NULL OR update_policy = 'current_version')",
        "ALTER TABLE access_codes ADD COLUMN course_version TEXT",
        "ALTER TABLE access_codes ADD COLUMN package_hash TEXT",
        "ALTER TABLE access_codes ADD COLUMN voided_at TEXT",
        "ALTER TABLE access_codes ADD COLUMN void_reason TEXT",
        "ALTER TABLE access_codes ADD COLUMN replaces_code_id INTEGER REFERENCES access_codes(id)",
        "ALTER TABLE access_codes ADD COLUMN legacy_state TEXT",
        "ALTER TABLE access_codes ADD COLUMN purpose TEXT",
        "ALTER TABLE access_codes ADD COLUMN verified_at TEXT",
        "ALTER TABLE access_codes ADD COLUMN verified_by INTEGER REFERENCES admins(id)",
        "ALTER TABLE access_codes ADD COLUMN verified_reason TEXT",
        "ALTER TABLE access_codes ADD COLUMN created_by INTEGER REFERENCES admins(id)",
        "ALTER TABLE access_codes ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK (typeof(revision) = 'integer' AND revision >= 1)",
        "CREATE UNIQUE INDEX access_codes_number ON access_codes(code_number) WHERE code_number IS NOT NULL",
        "CREATE UNIQUE INDEX access_codes_course ON access_codes(id, course_id)",
        """CREATE TABLE entitlements (
            id INTEGER PRIMARY KEY,
            course_id TEXT NOT NULL REFERENCES courses(course_id),
            product_id INTEGER REFERENCES products(id),
            order_id INTEGER UNIQUE REFERENCES orders(id),
            source_code_id INTEGER UNIQUE REFERENCES access_codes(id),
            course_version TEXT NOT NULL,
            package_hash TEXT,
            access_days INTEGER CHECK (access_days IS NULL OR (typeof(access_days) = 'integer' AND access_days BETWEEN 1 AND 3650)),
            update_policy TEXT NOT NULL DEFAULT 'current_version' CHECK (update_policy = 'current_version'),
            expires_at TEXT,
            revoked_at TEXT,
            revoke_reason TEXT,
            legacy_state TEXT,
            purpose TEXT,
            verified_at TEXT,
            verified_by INTEGER REFERENCES admins(id),
            verified_reason TEXT,
            revision INTEGER NOT NULL DEFAULT 1 CHECK (typeof(revision) = 'integer' AND revision >= 1),
            created_by INTEGER REFERENCES admins(id),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')),
            UNIQUE (id, course_id),
            FOREIGN KEY (product_id, course_id) REFERENCES products(id, course_id),
            FOREIGN KEY (order_id, product_id) REFERENCES orders(id, product_id),
            FOREIGN KEY (source_code_id, course_id) REFERENCES access_codes(id, course_id),
            CHECK (order_id IS NULL OR product_id IS NOT NULL)
        )""",
        """CREATE TABLE recovery_credentials (
            id INTEGER PRIMARY KEY,
            entitlement_id INTEGER NOT NULL REFERENCES entitlements(id),
            credential_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')),
            revoked_at TEXT,
            created_by INTEGER REFERENCES admins(id),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (typeof(revision) = 'integer' AND revision >= 1)
        )""",
        "CREATE UNIQUE INDEX recovery_one_unrevoked ON recovery_credentials(entitlement_id) WHERE revoked_at IS NULL",
        "ALTER TABLE sessions ADD COLUMN entitlement_id INTEGER REFERENCES entitlements(id)",
        "ALTER TABLE sessions ADD COLUMN source_code_id INTEGER REFERENCES access_codes(id)",
        "ALTER TABLE sessions ADD COLUMN csrf_hash TEXT",
        "ALTER TABLE sessions ADD COLUMN revoked_at TEXT",
        """CREATE TABLE entitlement_progress (
            entitlement_id INTEGER NOT NULL,
            course_id TEXT NOT NULL,
            chapter_number INTEGER NOT NULL,
            completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (entitlement_id, course_id, chapter_number),
            FOREIGN KEY (entitlement_id, course_id) REFERENCES entitlements(id, course_id),
            FOREIGN KEY (course_id, chapter_number) REFERENCES chapters(course_id, chapter_number)
        )""",
        """CREATE TABLE admin_events (
            id INTEGER PRIMARY KEY,
            actor_admin_id INTEGER REFERENCES admins(id),
            object_type TEXT NOT NULL,
            object_id TEXT NOT NULL,
            action TEXT NOT NULL,
            reason TEXT NOT NULL,
            changes_json TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN ('success', 'denied')),
            request_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE csrf_challenges (
            nonce_hash TEXT PRIMARY KEY NOT NULL,
            scope TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT
        )""",
        """CREATE TABLE operation_requests (
            id INTEGER PRIMARY KEY,
            actor_admin_id INTEGER NOT NULL REFERENCES admins(id),
            action TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            object_type TEXT NOT NULL,
            object_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')),
            UNIQUE (actor_admin_id, action, idempotency_key)
        )""",
        # Preserve the old table/rows and enforce booleans on future writes.
        """CREATE TRIGGER progress_boolean_insert BEFORE INSERT ON progress
            WHEN NEW.completed NOT IN (0, 1)
            BEGIN SELECT RAISE(ABORT, 'completed must be boolean'); END""",
        """CREATE TRIGGER progress_boolean_update BEFORE UPDATE OF completed ON progress
            WHEN NEW.completed NOT IN (0, 1)
            BEGIN SELECT RAISE(ABORT, 'completed must be boolean'); END""",
    )
    for statement in statements:
        connection.execute(statement)

    # SQLite cannot add composite FKs with ALTER TABLE. Preserve the legacy
    # tables and apply the equivalent checks to writes to their new links.
    links = {
        "access_codes": """
            (NEW.product_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM products WHERE id=NEW.product_id AND course_id=NEW.course_id))
            OR (NEW.batch_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM code_batches WHERE id=NEW.batch_id AND product_id=NEW.product_id))
            OR (NEW.order_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM orders WHERE id=NEW.order_id AND product_id=NEW.product_id))
        """,
        "sessions": """
            (NEW.entitlement_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM entitlements WHERE id=NEW.entitlement_id AND course_id=NEW.course_id))
            OR (NEW.source_code_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM access_codes WHERE id=NEW.source_code_id AND course_id=NEW.course_id))
        """,
    }
    for table, condition in links.items():
        for operation in ("INSERT", "UPDATE"):
            connection.execute(f"""
                CREATE TRIGGER {table}_links_{operation.lower()} BEFORE {operation} ON {table}
                WHEN {condition}
                BEGIN SELECT RAISE(ABORT, 'inconsistent course or product link'); END
            """)

    # These legacy-table links also need the parent side of a composite FK:
    # a valid child must not become inconsistent when its parent is updated.
    reverse_links = {
        "products": """EXISTS (
            SELECT 1 FROM access_codes
            WHERE product_id=OLD.id AND course_id IS NOT NEW.course_id)""",
        "orders": """EXISTS (
            SELECT 1 FROM access_codes
            WHERE order_id=OLD.id AND product_id IS NOT NEW.product_id)""",
        "code_batches": """EXISTS (
            SELECT 1 FROM access_codes
            WHERE batch_id=OLD.id AND product_id IS NOT NEW.product_id)""",
        "entitlements": """EXISTS (
            SELECT 1 FROM sessions
            WHERE entitlement_id=OLD.id AND course_id IS NOT NEW.course_id)""",
        "access_codes": """EXISTS (
            SELECT 1 FROM sessions
            WHERE source_code_id=OLD.id AND course_id IS NOT NEW.course_id)""",
    }
    for table, condition in reverse_links.items():
        connection.execute(f"""
            CREATE TRIGGER {table}_reverse_links_update BEFORE UPDATE ON {table}
            WHEN {condition}
            BEGIN SELECT RAISE(ABORT, 'inconsistent course or product link'); END
        """)
