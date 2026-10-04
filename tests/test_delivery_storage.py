"""Additive v004 preserves the entire populated v003 graph and strict snapshots."""

import json
import sqlite3
from contextlib import closing

import pytest

from course_platform.database import connect, migrate_database, transaction
from course_platform.domain import BusinessError
from course_platform.migrations import v004_delivery_storage
from test_migrations import operations_snapshot, populated_v2  # noqa: F401


@pytest.fixture
def populated_v3(populated_v2, tmp_path):
    migrate_database(populated_v2, through_version=3, backup_path=tmp_path / "before-v3.db")
    with transaction(populated_v2) as connection:
        connection.execute("UPDATE orders SET status='refunded', refunded_at='original-refund', refund_reason='external-confirmed'")
    return populated_v2


def test_fresh_storage_and_historical_upgrade_preserve_all_columns(populated_v3, tmp_path):
    before = operations_snapshot(populated_v3)
    with closing(connect(populated_v3)) as connection:
        columns = {table: [row[1] for row in connection.execute(f"PRAGMA table_info({table})")] for table in before}
        retained_schema = [tuple(row) for row in connection.execute("SELECT type, name, sql FROM sqlite_master WHERE type IN ('index','trigger','view') ORDER BY type, name")]
    backup = tmp_path / "v3-backup.db"
    report = migrate_database(populated_v3, backup_path=backup, through_version=4)
    assert (report.from_version, report.to_version) == (3, 4)
    assert operations_snapshot(backup) == before
    with closing(connect(populated_v3)) as connection:
        for table, names in columns.items():
            old_rows = [tuple(row) for row in connection.execute(f"SELECT {', '.join(names)} FROM {table}")]
            assert old_rows[:len(before[table])] == before[table]
        for table in ("code_batches", "access_codes", "entitlements", "orders"):
            assert connection.execute(f"SELECT issued_policy_json FROM {table} WHERE id=1").fetchone()[0] is None
        assert connection.execute("SELECT revision FROM code_batches").fetchone()[0] == 1
        assert connection.execute("SELECT paid_at, delivery_state, delivered_at, status, refunded_at FROM orders").fetchone()[:] == (None, None, None, "refunded", "original-refund")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        current_schema = [tuple(row) for row in connection.execute("SELECT type, name, sql FROM sqlite_master WHERE type IN ('index','trigger','view') ORDER BY type, name")]
        assert all(row in current_schema for row in retained_schema)
    before_rerun = populated_v3.read_bytes()
    assert migrate_database(populated_v3, through_version=4).to_version == 4 and populated_v3.read_bytes() == before_rerun
    fresh = tmp_path / "fresh.db"
    assert migrate_database(fresh, through_version=4).to_version == 4
    with closing(connect(fresh)) as connection:
        assert [row[0] for row in connection.execute("SELECT version FROM schema_migrations")] == [1, 2, 3, 4]


def test_storage_requires_backup_and_rolls_back_ddl(populated_v3, tmp_path, monkeypatch):
    before = operations_snapshot(populated_v3)
    with closing(connect(populated_v3)) as connection:
        schema = connection.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        schema = [tuple(row) for row in schema]
    with pytest.raises(BusinessError) as caught:
        migrate_database(populated_v3, through_version=4)
    assert caught.value.code == "backup_required"
    original = v004_delivery_storage.apply

    def fail(connection):
        original(connection)
        connection.execute("UPDATE code_batches SET notes='partial'")
        raise RuntimeError("storage failure")

    monkeypatch.setattr(v004_delivery_storage, "apply", fail)
    backup = tmp_path / "rollback.db"
    with pytest.raises(RuntimeError, match="storage failure"):
        migrate_database(populated_v3, backup_path=backup, through_version=4)
    assert operations_snapshot(populated_v3) == operations_snapshot(backup) == before
    with closing(connect(populated_v3)) as connection:
        assert [tuple(row) for row in connection.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name")] == schema
    monkeypatch.setattr(v004_delivery_storage, "apply", original)
    assert migrate_database(populated_v3, backup_path=tmp_path / "retry.db", through_version=4).to_version == 4


POLICY = {"product_id": 1, "course_id": "c1", "course_slug": "c1", "version": "1.0.0",
    "package_hash": "a" * 64, "title": "已承诺课程", "support_text": "邮件支持",
    "access": {"access_mode": "days", "access_days": 30, "online": True, "pdf": False, "zip": False, "update_policy": "current_version"}}


@pytest.mark.parametrize("table", ["code_batches", "access_codes", "entitlements", "orders"])
@pytest.mark.parametrize("mutation", ["valid", "no_expiry", "null", "malformed", "missing", "extra", "flag", "days", "duration", "identity", "secret", "secret_escaped"])
def test_storage_snapshots_are_strict(populated_v3, tmp_path, table, mutation):
    migrate_database(populated_v3, backup_path=tmp_path / "v3.db", through_version=4)
    policy = json.loads(json.dumps(POLICY))
    if mutation == "no_expiry":
        policy["access"].update(access_mode="no_fixed_expiry", access_days=None)
    elif mutation == "missing":
        policy.pop("support_text")
    elif mutation == "extra":
        policy["raw_code"] = "secret"
    elif mutation == "flag":
        policy["access"]["online"] = 1
    elif mutation == "days":
        policy["access"]["access_days"] = True
    elif mutation == "duration":
        policy["access"].update(access_mode="no_fixed_expiry", access_days=30)
    elif mutation == "identity":
        policy["product_id"] = "1"
    elif mutation in ("secret", "secret_escaped"):
        policy["title"] = "CS-plaintext"
    value = None if mutation == "null" else "{" if mutation == "malformed" else json.dumps(policy)
    if mutation == "secret_escaped":
        value = value.replace("CS-", r"\u0043S-")
    with transaction(populated_v3) as connection:
        if mutation in ("valid", "no_expiry", "null"):
            connection.execute(f"UPDATE {table} SET issued_policy_json=?", (value,))
            assert connection.execute(f"SELECT issued_policy_json FROM {table} WHERE id=1").fetchone()[0] == value
        else:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(f"UPDATE {table} SET issued_policy_json=?", (value,))


@pytest.mark.parametrize("statement", ["UPDATE code_batches SET revision=0", "UPDATE code_batches SET revision=1.5",
    "UPDATE orders SET delivery_state='invalid'"])
def test_storage_revision_and_lifecycle_constraints(populated_v3, tmp_path, statement):
    migrate_database(populated_v3, backup_path=tmp_path / "v3.db", through_version=4)
    with transaction(populated_v3) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(statement)


def test_owner_readiness_rejects_older_schema_without_upgrading(populated_v3):
    from course_platform.admin.auth import AdminService

    before = populated_v3.read_bytes()
    with pytest.raises(BusinessError) as caught:
        AdminService(populated_v3).require_initialized()
    assert caught.value.code == "admin_unavailable" and populated_v3.read_bytes() == before
