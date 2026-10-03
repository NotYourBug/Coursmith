"""Real SQLite tests: lost rows, partial commits, unsafe backup and invalid writes."""

import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace

import pytest

from course_platform import cli
from course_platform.database import backup_database, connect, migrate_database, transaction
from course_platform.domain import BusinessError
from course_platform.migrations import v002_operations


def legacy_snapshot(path):
    with closing(sqlite3.connect(path)) as connection:
        return {
            table: ([column[1] for column in connection.execute(f"PRAGMA table_info({table})")],
                    connection.execute(f"SELECT * FROM {table}").fetchall())
            for table in ("courses", "chapters", "access_codes", "sessions", "progress", "events")
        }


def test_migration_preserves_legacy_tables_and_is_repeatable(legacy_db, tmp_path):
    before = legacy_snapshot(legacy_db)
    backup = tmp_path / "before.db"
    first = migrate_database(legacy_db, backup_path=backup, through_version=2)
    second = migrate_database(legacy_db, through_version=2)
    assert (first.from_version, first.to_version, first.backup_path) == (0, 2, backup)
    assert (second.from_version, second.to_version, second.backup_path) == (2, 2, None)
    assert legacy_snapshot(backup) == before
    with transaction(legacy_db) as connection:
        for table, (columns, rows) in before.items():
            assert [tuple(row) for row in connection.execute(f"SELECT {', '.join(columns)} FROM {table}")] == rows
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()[0][0] == 1
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 2
        assert connection.execute("SELECT package_hash FROM courses").fetchone()[0] is None
        assert connection.execute("SELECT COUNT(*) FROM entitlements").fetchone()[0] == 0


def test_failed_migration_rolls_back(legacy_db, tmp_path, monkeypatch):
    before = legacy_snapshot(legacy_db)
    original = v002_operations.apply

    def fail_midway(connection):
        original(connection)
        connection.execute("UPDATE courses SET title='partial'")
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(v002_operations, "apply", fail_midway)
    backup = tmp_path / "recover.db"
    with pytest.raises(RuntimeError, match="injected"):
        migrate_database(legacy_db, backup_path=backup)
    assert legacy_snapshot(legacy_db) == before
    assert legacy_snapshot(backup) == before
    with closing(sqlite3.connect(legacy_db)) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name IN ('schema_migrations', 'products')").fetchall() == []
    monkeypatch.setattr(v002_operations, "apply", original)
    assert migrate_database(legacy_db, backup_path=tmp_path / "retry.db").to_version == 3


def test_existing_upgrade_requires_non_overwriting_backup(legacy_db, tmp_path):
    before = legacy_db.read_bytes()
    with pytest.raises(BusinessError):
        migrate_database(legacy_db)
    backup = tmp_path / "occupied.db"
    backup.write_bytes(b"keep this file")
    for target in (legacy_db, backup):
        with pytest.raises(BusinessError):
            migrate_database(legacy_db, backup_path=target)
    assert legacy_db.read_bytes() == before
    assert backup.read_bytes() == b"keep this file"


def test_backup_uses_consistent_sqlite_snapshot_and_validates(legacy_db, tmp_path):
    with closing(connect(legacy_db)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE courses SET title='WAL commit'")
        writer.commit()
        target = tmp_path / "snapshot.db"
        backup_database(legacy_db, target)
        with closing(sqlite3.connect(target)) as restored:
            assert restored.execute("SELECT title FROM courses").fetchone()[0] == "WAL commit"
            assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_backup_rejects_broken_foreign_keys(legacy_db, tmp_path):
    with closing(sqlite3.connect(legacy_db)) as connection:
        connection.execute("UPDATE sessions SET course_id='missing'")
        connection.commit()
    target = tmp_path / "invalid.db"
    with pytest.raises(BusinessError):
        backup_database(legacy_db, target)
    assert not target.exists()


def test_v1_can_be_upgraded_separately(tmp_path):
    path = tmp_path / "new.db"
    assert migrate_database(path, through_version=1).to_version == 1
    with pytest.raises(BusinessError):
        migrate_database(path)
    report = migrate_database(path, backup_path=tmp_path / "v1.db")
    assert (report.from_version, report.to_version) == (1, 3)


@pytest.mark.parametrize("versions", [[3], [1, 3], [2]])
def test_unknown_or_noncontiguous_versions_are_rejected(tmp_path, versions):
    path = tmp_path / "unknown.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        connection.executemany("INSERT INTO schema_migrations VALUES (?, '2026-10-02')", [(v,) for v in versions])
        connection.commit()
    before = path.read_bytes()
    with pytest.raises(BusinessError):
        migrate_database(path, backup_path=tmp_path / "before.db")
    assert path.read_bytes() == before


@pytest.mark.parametrize("version", [0, 4, -1, True, 1.5])
def test_invalid_target_is_rejected_before_creating_database(tmp_path, version):
    path = tmp_path / "new.db"
    with pytest.raises(BusinessError):
        migrate_database(path, through_version=version)
    assert not path.exists()


def test_transaction_commits_rolls_back_and_closes(db_path):
    with transaction(db_path, immediate=True) as connection:
        assert connection.in_transaction
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 3000
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        connection.execute("INSERT INTO categories (slug, name) VALUES ('ai', 'AI')")
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    with pytest.raises(RuntimeError):
        with transaction(db_path) as failed:
            failed.execute("UPDATE categories SET name='lost'")
            raise RuntimeError("rollback")
    with pytest.raises(sqlite3.ProgrammingError):
        failed.execute("SELECT 1")
    with transaction(db_path) as connection:
        assert connection.execute("SELECT name FROM categories").fetchone()[0] == "AI"


def test_busy_returns_bounded_error(db_path):
    with transaction(db_path, immediate=True):
        start = time.monotonic()
        with pytest.raises(BusinessError) as caught:
            with transaction(db_path, immediate=True):
                pytest.fail("locked writer was admitted")
        elapsed = time.monotonic() - start
    assert caught.value.code == "database_busy"
    assert caught.value.status_code == 503
    assert 2.5 <= elapsed < 6
    with transaction(db_path, immediate=True) as connection:
        connection.execute("INSERT INTO categories (slug, name) VALUES ('retry', 'Retry')")


def test_cli_check_only_does_not_modify_source(legacy_db, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(database_path=legacy_db))
    before = legacy_db.read_bytes()
    assert cli.main(["migrate", "--check-only"]) == 0
    output = capsys.readouterr().out
    assert '"version": 0' in output and '"integrity": "ok"' in output
    assert legacy_db.read_bytes() == before
    assert cli.main(["migrate", "--backup", str(tmp_path / "before.db")]) == 0
    assert '"to_version": 3' in capsys.readouterr().out


def test_cli_check_missing_database_does_not_create_it(tmp_path, monkeypatch, capsys):
    path = tmp_path / "missing.db"
    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(database_path=path))
    assert cli.main(["migrate", "--check-only"]) == 1
    assert not path.exists()
    assert "missing" in capsys.readouterr().out


def test_cli_check_detects_unknown_versions_and_fk_damage(legacy_db, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(database_path=legacy_db))
    with closing(sqlite3.connect(legacy_db)) as connection:
        connection.execute("UPDATE sessions SET course_id='missing'")
        connection.commit()
    assert cli.main(["migrate", "--check-only"]) == 1
    assert "foreign_key" in capsys.readouterr().out


@pytest.fixture
def seeded_operations(db_path):
    with transaction(db_path) as connection:
        connection.execute("INSERT INTO admins (id, username, password_hash) VALUES (1, 'owner', 'argon2-hash')")
        connection.execute("INSERT INTO courses (course_id, slug, title, category, version, status, content_path, updated_at) VALUES ('c1', 'one', 'One', 'AI', '1', 'published', 'path', '2026-10-02')")
        connection.execute("INSERT INTO chapters VALUES ('c1', 1, 'One', 'one.html', 0)")
        connection.execute("INSERT INTO products (id, course_id, title, status, created_by) VALUES (1, 'c1', 'One', 'draft', 1)")
        connection.execute("INSERT INTO orders (id, product_id, channel, shop, external_order_id, amount_cents, status, created_by) VALUES (1, 1, 'store', 'shop', '001', 1200, 'paid', 1)")
        connection.execute("INSERT INTO access_codes (id, course_id, code_hash, created_at, code_number, product_id) VALUES (1, 'c1', 'code-hash', '2026-10-02', 'C001', 1)")
        connection.execute("INSERT INTO entitlements (id, course_id, product_id, order_id, source_code_id, course_version, created_by) VALUES (1, 'c1', 1, 1, 1, '1', 1)")
    return db_path


@pytest.mark.parametrize("statement", [
    "INSERT INTO products (course_id, title, created_by) VALUES ('c1', 'Duplicate', 1)",
    "UPDATE products SET status='invalid'",
    "UPDATE products SET access_days=0",
    "UPDATE products SET access_days=3651",
    "UPDATE products SET update_policy='automatic'",
    "UPDATE products SET revision=-1",
    "UPDATE orders SET amount_cents=-1",
    "UPDATE orders SET amount_cents=1.5",
    "UPDATE orders SET status='invalid'",
    "UPDATE orders SET created_by=999",
    "INSERT INTO orders (product_id, channel, shop, external_order_id, amount_cents, created_by) VALUES (1, 'store', 'shop', '001', 100, 1)",
    "INSERT INTO access_codes (course_id, code_hash, created_at, code_number) VALUES ('c1', 'other', 'now', 'C001')",
    "INSERT INTO entitlements (course_id, product_id, source_code_id, course_version, created_by) VALUES ('c1', 1, 1, '1', 1)",
    "INSERT INTO entitlements (course_id, product_id, order_id, course_version, created_by) VALUES ('c1', 1, 1, '1', 1)",
    "INSERT INTO entitlement_progress VALUES (1, 'c1', 1, 2, 'now')",
    "INSERT INTO entitlement_progress VALUES (1, 'c1', 2, 1, 'now')",
    "UPDATE admins SET role='staff'",
    "UPDATE admins SET enabled=2",
    "INSERT INTO code_batches (product_id, quantity, purpose, idempotency_key, created_by) VALUES (1, 0, 'sale', 'key', 1)",
    "INSERT INTO code_batches (product_id, quantity, purpose, idempotency_key, created_by) VALUES (1, 201, 'sale', 'key', 1)",
    "INSERT INTO code_batches (product_id, quantity, purpose, idempotency_key, activation_days, created_by) VALUES (1, 1, 'sale', 'key', 366, 1)",
])
def test_operations_constraints_reject_invalid_writes(seeded_operations, statement):
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(seeded_operations) as connection:
            connection.execute(statement)


def test_nullable_drafts_and_orders_and_revoked_recovery_history(seeded_operations):
    with transaction(seeded_operations) as connection:
        connection.execute("INSERT INTO products (title, created_by) VALUES ('Unbound draft', 1)")
        connection.execute("INSERT INTO products (title, created_by) VALUES ('Another draft', 1)")
        connection.execute("INSERT INTO entitlements (course_id, product_id, course_version, created_by) VALUES ('c1', 1, '1', 1)")
        connection.execute("INSERT INTO entitlements (course_id, product_id, course_version, created_by) VALUES ('c1', 1, '1', 1)")
        connection.execute("INSERT INTO recovery_credentials (entitlement_id, credential_hash, created_by) VALUES (1, 'first-hash', 1)")
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(seeded_operations) as connection:
            connection.execute("INSERT INTO recovery_credentials (entitlement_id, credential_hash, created_by) VALUES (1, 'second-hash', 1)")
    with transaction(seeded_operations) as connection:
        connection.execute("UPDATE recovery_credentials SET revoked_at='now'")
        connection.execute("INSERT INTO recovery_credentials (entitlement_id, credential_hash, created_by) VALUES (1, 'second-hash', 1)")
        assert connection.execute("SELECT COUNT(*) FROM recovery_credentials").fetchone()[0] == 2


def test_operation_requests_and_csrf_persist_only_hashes_and_metadata(seeded_operations):
    with transaction(seeded_operations) as connection:
        connection.execute("INSERT INTO operation_requests (actor_admin_id, action, idempotency_key, request_digest, object_type, object_id) VALUES (1, 'code.issue', 'key', 'digest', 'batch', '1')")
        connection.execute("INSERT INTO csrf_challenges (nonce_hash, scope, expires_at) VALUES ('nonce-hash', 'admin-login', '2026-10-02T00:10:00+00:00')")
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(seeded_operations) as connection:
            connection.execute("INSERT INTO operation_requests (actor_admin_id, action, idempotency_key, request_digest, object_type, object_id) VALUES (1, 'code.issue', 'key', 'another', 'batch', '2')")


def test_legacy_progress_boolean_constraint_is_enforced(legacy_db, tmp_path):
    migrate_database(legacy_db, backup_path=tmp_path / "before.db")
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(legacy_db) as connection:
            connection.execute("UPDATE progress SET completed=2")


def test_owner_account_is_singleton(db_path):
    with transaction(db_path) as connection:
        connection.execute("INSERT INTO admins (username, password_hash) VALUES ('owner', 'hash')")
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(db_path) as connection:
            connection.execute("INSERT INTO admins (username, password_hash) VALUES ('second-owner', 'hash')")


@pytest.mark.parametrize("statement", [
    "UPDATE access_codes SET course_id='c2' WHERE id=1",
    "INSERT INTO sessions (session_hash, course_id, created_at, expires_at, entitlement_id) VALUES ('wrong-session', 'c2', 'now', 'later', 1)",
    "INSERT INTO sessions (session_hash, course_id, created_at, expires_at, source_code_id) VALUES ('wrong-code', 'c2', 'now', 'later', 1)",
    "INSERT INTO entitlements (course_id, product_id, course_version) VALUES ('c2', 1, '1')",
    "INSERT INTO entitlement_progress VALUES (1, 'c2', 1, 1, 'now')",
])
def test_cross_course_links_are_rejected(seeded_operations, statement):
    with transaction(seeded_operations) as connection:
        connection.execute("INSERT INTO courses (course_id, slug, title, category, version, status, content_path, updated_at) VALUES ('c2', 'two', 'Two', 'AI', '1', 'published', 'path', '2026-10-02')")
        connection.execute("INSERT INTO chapters VALUES ('c2', 1, 'Two', 'two.html', 0)")
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(seeded_operations) as connection:
            connection.execute(statement)


@pytest.mark.parametrize("statement", [
    "UPDATE access_codes SET product_id=2 WHERE id=1",
    "UPDATE access_codes SET order_id=2 WHERE id=1",
    "UPDATE access_codes SET batch_id=2 WHERE id=1",
    "UPDATE entitlements SET order_id=2 WHERE id=1",
])
def test_cross_product_links_are_rejected(seeded_operations, statement):
    with transaction(seeded_operations) as connection:
        connection.execute("INSERT INTO products (id, title, created_by) VALUES (2, 'Other draft', 1)")
        connection.execute("INSERT INTO orders (id, product_id, channel, shop, external_order_id, amount_cents, created_by) VALUES (2, 2, 'store', 'shop', '002', 100, 1)")
        connection.execute("INSERT INTO code_batches (id, product_id, quantity, purpose, idempotency_key, created_by) VALUES (2, 2, 1, 'sale', 'other-key', 1)")
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(seeded_operations) as connection:
            connection.execute(statement)


@pytest.mark.parametrize("statement", [
    "UPDATE entitlements SET course_id='c2', product_id=NULL, order_id=NULL, source_code_id=NULL WHERE id=1",
    "UPDATE access_codes SET course_id='c2', product_id=NULL WHERE id=3",
    "UPDATE orders SET product_id=2 WHERE id=2",
    "UPDATE code_batches SET product_id=2 WHERE id=2",
    "UPDATE products SET course_id='c2' WHERE id=2",
])
def test_parent_updates_cannot_invalidate_legacy_table_links(seeded_operations, statement):
    # Removing reverse-link protection would leave an existing session or code
    # pointing at a parent whose course/product no longer agrees with it.
    with transaction(seeded_operations) as connection:
        connection.execute("INSERT INTO courses (course_id, slug, title, category, version, status, content_path, updated_at) VALUES ('c2', 'two', 'Two', 'AI', '1', 'published', 'path', 'now')")
        connection.execute("INSERT INTO sessions (session_hash, course_id, created_at, expires_at, entitlement_id, source_code_id) VALUES ('linked', 'c1', 'now', 'later', 1, 1)")
        connection.execute("INSERT INTO products (id, title, created_by) VALUES (2, 'Unbound draft', 1)")
        connection.execute("INSERT INTO orders (id, product_id, channel, shop, external_order_id, amount_cents, created_by) VALUES (2, 1, 'store', 'shop', '002', 100, 1)")
        connection.execute("INSERT INTO code_batches (id, product_id, quantity, purpose, idempotency_key, created_by) VALUES (2, 1, 1, 'sale', 'key', 1)")
        connection.execute("INSERT INTO access_codes (id, course_id, code_hash, created_at, product_id, order_id, batch_id) VALUES (2, 'c1', 'another', 'now', 1, 2, 2)")
        # Bind another product to a course with no entitlement/composite FK
        # protection, so the trigger itself has to enforce the code link.
        connection.execute("INSERT INTO courses (course_id, slug, title, category, version, status, content_path, updated_at) VALUES ('c3', 'three', 'Three', 'AI', '1', 'published', 'path', 'now')")
        connection.execute("UPDATE products SET course_id='c3' WHERE id=2")
        connection.execute("INSERT INTO access_codes (id, course_id, code_hash, created_at, product_id) VALUES (3, 'c3', 'third', 'now', 2)")
        connection.execute("INSERT INTO sessions (session_hash, course_id, created_at, expires_at, source_code_id) VALUES ('code-only', 'c3', 'now', 'later', 3)")
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(seeded_operations) as connection:
            connection.execute(statement)


def test_order_entitlement_requires_matching_product(seeded_operations):
    # A NULL component bypasses a composite FK; an order may not lose its
    # product association by clearing only the entitlement's product field.
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(seeded_operations) as connection:
            connection.execute("UPDATE entitlements SET product_id=NULL WHERE id=1")


def operations_snapshot(path):
    with closing(connect(path)) as connection:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
                for table in tables}


@pytest.fixture
def populated_v2(tmp_path):
    path = tmp_path / "populated-v2.db"
    migrate_database(path, through_version=2)
    with transaction(path) as connection:
        connection.execute("INSERT INTO admins (id, username, password_hash) VALUES (1, 'owner', 'hash')")
        connection.execute("INSERT INTO categories (id, slug, name, created_by) VALUES (1, 'technical', '技术', 1)")
        for course_id in ("c1", "c2", "c3"):
            connection.execute("""INSERT INTO courses
                (course_id, slug, title, category, version, status, content_path, updated_at, package_hash)
                VALUES (?, ?, 'Course', 'technical', '1.0.0', 'published', 'original/path', '2026-10-02', 'original-hash')""",
                (course_id, course_id))
        connection.execute("INSERT INTO chapters VALUES ('c1', 1, 'One', 'one.html', 0)")
        connection.execute("""INSERT INTO products
            (id, course_id, title, description, category_id, status, access_days, update_policy,
             sales_check_json, sales_checked_at, sales_package_hash, revision, created_by, created_at, updated_at)
            VALUES (1, 'c1', 'Original', 'unchanged-json', 1, 'active', 30, 'current_version',
                    'original-approval', '2026-10-02', 'original-hash', 7, 1, 'created', 'updated')""")
        connection.execute("INSERT INTO products (id, course_id, title, created_by) VALUES (2, 'c3', 'Code-only', 1)")
        connection.execute("INSERT INTO products (id, title, created_by) VALUES (3, 'Unbound', 1)")
        connection.execute("""INSERT INTO orders
            (id, product_id, channel, shop, external_order_id, amount_cents, created_by)
            VALUES (1, 1, 'store', 'shop', '001', 1200, 1)""")
        connection.execute("""INSERT INTO code_batches
            (id, product_id, quantity, purpose, idempotency_key, access_days, course_version, package_hash, created_by)
            VALUES (1, 1, 1, 'sale', 'batch-key', 30, '1.0.0', 'original-hash', 1)""")
        connection.execute("""INSERT INTO access_codes
            (id, course_id, code_hash, created_at, used_at, product_id, batch_id, order_id,
             access_days, course_version, package_hash, update_policy, created_by)
            VALUES (1, 'c1', 'code-hash', 'created', 'used', 1, 1, 1, 30, '1.0.0', 'original-hash', 'current_version', 1)""")
        connection.execute("INSERT INTO access_codes (id, course_id, code_hash, created_at, product_id) VALUES (2, 'c3', 'other-code', 'created', 2)")
        connection.execute("""INSERT INTO entitlements
            (id, course_id, product_id, order_id, source_code_id, course_version, package_hash, access_days, created_by)
            VALUES (1, 'c1', 1, 1, 1, '1.0.0', 'original-hash', 30, 1)""")
        connection.execute("""INSERT INTO sessions
            (session_hash, course_id, created_at, expires_at, entitlement_id, source_code_id)
            VALUES ('session-hash', 'c1', 'created', 'original-expiry', 1, 1)""")
        connection.execute("INSERT INTO progress VALUES ('session-hash', 'c1', 1, 1, 'original-time')")
        connection.execute("INSERT INTO entitlement_progress VALUES (1, 'c1', 1, 1, 'original-time')")
        connection.execute("INSERT INTO recovery_credentials (entitlement_id, credential_hash, created_by) VALUES (1, 'recovery-hash', 1)")
        connection.execute("INSERT INTO admin_sessions VALUES ('admin-hash', 1, 'csrf-hash', 'created', 'activity', 'expiry', NULL)")
        connection.execute("INSERT INTO csrf_challenges VALUES ('nonce-hash', 'admin.login', 'expiry', NULL)")
        connection.execute("INSERT INTO request_limits VALUES ('login', 'source-hash', 'owner', 'window', 1, NULL)")
        connection.execute("INSERT INTO events (event_type, course_id, metadata_json, created_at) VALUES ('redeemed', 'c1', '{}', 'created')")
        connection.execute("""INSERT INTO admin_events
            (actor_admin_id, object_type, object_id, action, reason, changes_json, outcome, request_id, created_at)
            VALUES (1, 'product', '1', 'product.create', 'completed', '{}', 'success', 'original-request', 'created')""")
        connection.execute("""INSERT INTO operation_requests
            (actor_admin_id, action, idempotency_key, request_digest, object_type, object_id)
            VALUES (1, 'code.issue', 'original-key', 'original-digest', 'batch', '1')""")
        connection.execute("CREATE INDEX product_title_lookup ON products(title)")
        connection.execute("CREATE VIEW product_titles AS SELECT id, title FROM products")
    return path


def test_product_lifecycle_fresh_allows_import_and_unbound_archive(tmp_path):
    path = tmp_path / "fresh.db"
    assert migrate_database(path).to_version == 3
    with transaction(path) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        connection.execute("INSERT INTO products (title, created_by) VALUES ('Auto draft', NULL)")
        connection.execute("UPDATE products SET status='archived'")
        assert connection.execute("SELECT course_id, status, created_by FROM products").fetchone()[:] == (None, "archived", None)
        assert [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")] == [1, 2, 3]
        for status in ("active", "paused"):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("UPDATE products SET status=?", (status,))


def test_product_lifecycle_upgrade_preserves_populated_graph_and_backup(populated_v2, tmp_path):
    before = operations_snapshot(populated_v2)
    with closing(connect(populated_v2)) as connection:
        triggers = [tuple(row) for row in connection.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name")]
    backup = tmp_path / "before-v3.db"
    report = migrate_database(populated_v2, backup_path=backup)
    assert (report.from_version, report.to_version, report.backup_path) == (2, 3, backup)
    assert operations_snapshot(backup) == before
    after = operations_snapshot(populated_v2)
    assert after.pop("schema_migrations")[:2] == before.pop("schema_migrations")
    assert after == before
    with closing(connect(populated_v2)) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert [tuple(row) for row in connection.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name")] == triggers
        assert connection.execute("SELECT title FROM product_titles WHERE id=1").fetchone()[0] == "Original"
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='product_title_lookup'").fetchone()
    bytes_before_rerun = populated_v2.read_bytes()
    assert migrate_database(populated_v2).to_version == 3
    assert populated_v2.read_bytes() == bytes_before_rerun


def test_product_lifecycle_restores_views_before_their_triggers(populated_v2, tmp_path):
    with transaction(populated_v2) as connection:
        connection.execute("""CREATE TRIGGER product_titles_update
            INSTEAD OF UPDATE OF title ON product_titles BEGIN
            UPDATE products SET title=NEW.title WHERE id=OLD.id; END""")
    failure = None
    try:
        migrate_database(populated_v2, backup_path=tmp_path / "before-view.db")
    except sqlite3.OperationalError as error:
        failure = str(error)
    assert failure is None, f"Migration must preserve an existing view and its trigger: {failure}"
    with transaction(populated_v2) as connection:
        connection.execute("UPDATE product_titles SET title='Updated through view' WHERE id=1")
        assert connection.execute("SELECT title FROM products WHERE id=1").fetchone()[0] == "Updated through view"


@pytest.mark.parametrize("fault", ["exception", "invalid_fk", "invalid_legacy_link"])
def test_product_lifecycle_failure_rolls_back_graph_history_and_schema(populated_v2, tmp_path, monkeypatch, fault):
    from course_platform.migrations import v003_product_lifecycle

    before = operations_snapshot(populated_v2)
    with closing(connect(populated_v2)) as connection:
        schema_before = [tuple(row) for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")]
    original = v003_product_lifecycle.apply

    def fail_after_rebuild(connection):
        original(connection)
        connection.execute("UPDATE products SET title='partial'")
        if fault == "exception":
            raise RuntimeError("injected lifecycle failure")
        if fault == "invalid_fk":
            connection.execute("UPDATE categories SET created_by=999")
        else:
            connection.execute("DROP TRIGGER access_codes_links_update")
            connection.execute("UPDATE access_codes SET course_id='c2' WHERE id=2")

    monkeypatch.setattr(v003_product_lifecycle, "apply", fail_after_rebuild)
    backup = tmp_path / "recovery-v2.db"
    with pytest.raises(RuntimeError if fault == "exception" else BusinessError):
        migrate_database(populated_v2, backup_path=backup)
    assert operations_snapshot(populated_v2) == operations_snapshot(backup) == before
    with transaction(populated_v2) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert [tuple(row) for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")] == schema_before
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='products_lifecycle'").fetchone() is None
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE products SET status='archived' WHERE id=3")
    monkeypatch.setattr(v003_product_lifecycle, "apply", original)
    assert migrate_database(populated_v2, backup_path=tmp_path / "retry-v2.db").to_version == 3


def test_product_lifecycle_upgrade_requires_new_backup(populated_v2, tmp_path):
    before = operations_snapshot(populated_v2)
    occupied = tmp_path / "occupied.db"
    occupied.write_bytes(b"existing backup")
    for target in (None, populated_v2, occupied):
        with pytest.raises(BusinessError):
            migrate_database(populated_v2, backup_path=target)
        assert operations_snapshot(populated_v2) == before
    assert occupied.read_bytes() == b"existing backup"


@pytest.mark.parametrize("statement", [
    "UPDATE products SET course_id='c2' WHERE id=2",  # code-only parent trigger
    "UPDATE access_codes SET course_id='c2' WHERE id=2",  # child trigger
    "UPDATE products SET created_by=999 WHERE id=3",
    "UPDATE products SET access_days=0 WHERE id=3",
    "UPDATE products SET revision=0 WHERE id=3",
    "UPDATE products SET update_policy='automatic' WHERE id=3",
    "UPDATE products SET status='active' WHERE id=3",
    "UPDATE products SET status='paused' WHERE id=3",
    "UPDATE products SET status='invalid' WHERE id=3",
    "UPDATE products SET course_id='c1' WHERE id=3",
    "UPDATE products SET category_id=999 WHERE id=3",
])
def test_product_lifecycle_preserves_link_and_check_rejection(populated_v2, tmp_path, statement):
    assert migrate_database(populated_v2, backup_path=tmp_path / "before.db").to_version == 3
    with transaction(populated_v2) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(statement)
