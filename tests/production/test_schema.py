"""Schema 6 must preserve M1 facts and enforce production ownership/history."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from course_platform.database import check_database, migrate_database, open_readonly, transaction
from course_platform.operations.products import AccessPolicy, IssuedPolicy


# Explicit business tables and primary-key order; migration history is separate.
M1_TABLES = {
    "courses": "course_id", "chapters": "course_id, chapter_number",
    "access_codes": "id", "sessions": "session_hash",
    "progress": "session_hash, course_id, chapter_number", "events": "id",
    "admins": "id", "admin_sessions": "token_hash",
    "request_limits": "scope, source_digest, account_key", "categories": "id",
    "products": "id", "orders": "id", "code_batches": "id", "entitlements": "id",
    "recovery_credentials": "id", "entitlement_progress": "entitlement_id, course_id, chapter_number",
    "admin_events": "id", "csrf_challenges": "nonce_hash", "operation_requests": "id",
    "legacy_code_origins": "code_id", "legacy_entitlement_origins": "entitlement_id",
    "legacy_verifications": "id",
}
PRODUCTION_TABLES = {
    "production_projects", "brief_revisions", "reference_revisions", "project_reference_links",
    "outline_revisions", "outline_items", "outline_confirmations", "production_chapters",
    "chapter_revisions", "production_candidates", "draft_checks", "production_snapshots",
    "production_snapshot_refs", "production_jobs", "job_attempts", "job_steps", "model_calls",
    "token_reservations", "job_requests", "worker_heartbeats",
}
NOW = "2026-10-08T12:00:00+00:00"


def insert(connection, table, **values):
    columns = ", ".join(values)
    markers = ", ".join("?" for _ in values)
    connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({markers})", tuple(values.values()))


def read_m1_graph(path: Path) -> dict[str, list[tuple]]:
    with closing(open_readonly(path)) as connection:
        return {table: [tuple(row) for row in connection.execute(
            f"SELECT * FROM {table} ORDER BY {order}")] for table, order in M1_TABLES.items()}


def history(path):
    with closing(open_readonly(path)) as connection:
        return [tuple(row) for row in connection.execute("SELECT * FROM schema_migrations ORDER BY version")]


def seed_v5_graph(path: Path) -> dict[str, list[tuple]]:
    policy = IssuedPolicy(product_id=1, course_id="m1", course_slug="m1", version="1.0",
        package_hash="fixture-package-hash", title="M1 fixture", support_text="Fixture support",
        access=AccessPolicy(access_mode="days", access_days=30, online=True, pdf=False, zip=False,
                            update_policy="current_version"))
    payload = policy.model_dump_json()
    assert IssuedPolicy.model_validate_json(payload) == policy
    with transaction(path) as c:
        insert(c, "admins", id=1, username="fixture-owner", password_hash="fake-password-hash", created_at=NOW)
        insert(c, "categories", id=1, slug="fixture", name="Fixture", created_by=1)
        insert(c, "courses", course_id="m1", slug="m1", title="M1 fixture", category="fixture",
               version="1.0", status="published", content_path="fixture-only-path", updated_at=NOW,
               package_hash="fixture-package-hash")
        insert(c, "chapters", course_id="m1", chapter_number=1, title="Intro", path="intro.html", free_preview=0)
        insert(c, "products", id=1, course_id="m1", title="M1 fixture", category_id=1, created_by=1,
               status="active", access_days=30, update_policy="current_version", sales_check_json='{"fixture":true}',
               sales_checked_at=NOW, sales_package_hash="fixture-package-hash", revision=7, created_at=NOW)
        insert(c, "orders", id=1, product_id=1, channel="fixture", shop="fixture", external_order_id="001",
               amount_cents=1200, created_by=1, paid_at=NOW, delivery_state="activated", issued_policy_json=payload)
        insert(c, "code_batches", id=1, product_id=1, quantity=1, purpose="sale", idempotency_key="fixture-batch",
               created_by=1, access_days=30, course_version="1.0", package_hash="fixture-package-hash",
               issued_policy_json=payload)
        insert(c, "access_codes", id=1, course_id="m1", code_hash="fake-code-hash", code_number="FIXTURE001",
               created_at=NOW, used_at=NOW, product_id=1, order_id=1, batch_id=1, created_by=1,
               access_days=30, update_policy="current_version", course_version="1.0",
               package_hash="fixture-package-hash", issued_policy_json=payload)
        insert(c, "entitlements", id=1, course_id="m1", product_id=1, order_id=1, source_code_id=1,
               course_version="1.0", package_hash="fixture-package-hash", access_days=30,
               expires_at="2026-11-07T12:00:00+00:00", created_by=1, issued_policy_json=payload)
        insert(c, "recovery_credentials", id=1, entitlement_id=1, credential_hash="fake-recovery-hash", created_by=1)
        insert(c, "sessions", session_hash="fake-session-hash", course_id="m1", created_at=NOW,
               expires_at="2026-11-07T12:00:00+00:00", entitlement_id=1, source_code_id=1, csrf_hash="fake-csrf")
        insert(c, "progress", session_hash="fake-session-hash", course_id="m1", chapter_number=1,
               completed=1, updated_at=NOW)
        insert(c, "entitlement_progress", entitlement_id=1, course_id="m1", chapter_number=1, completed=1, updated_at=NOW)
        insert(c, "admin_sessions", token_hash="fake-admin-hash", admin_id=1, csrf_hash="fake-admin-csrf",
               created_at=NOW, last_activity_at=NOW, expires_at="2026-10-09T12:00:00+00:00")
        insert(c, "request_limits", scope="login", source_digest="fake-source", account_key="fixture-owner",
               window_started_at=NOW, count=1)
        insert(c, "csrf_challenges", nonce_hash="fake-nonce", scope="admin.login", expires_at=NOW)
        insert(c, "events", id=1, event_type="redeemed", course_id="m1", session_hash="fake-session-hash",
               metadata_json="{}", created_at=NOW)
        insert(c, "admin_events", id=1, actor_admin_id=1, object_type="product", object_id="1", action="product.create",
               reason="completed", changes_json="{}", outcome="success", request_id="fixture-request", created_at=NOW)
        insert(c, "operation_requests", id=1, actor_admin_id=1, action="code.issue", idempotency_key="fixture-key",
               request_digest="fixture-digest", object_type="batch", object_id="1")
        insert(c, "legacy_code_origins", code_id=1, code_hash="fake-code-hash", original_created_at=NOW,
               original_used_at=NOW, course_version="1.0", package_hash="fixture-package-hash", content_available=0)
        insert(c, "legacy_entitlement_origins", entitlement_id=1, session_hash="fake-session-hash",
               original_created_at=NOW, original_expires_at="2026-11-07T12:00:00+00:00", course_version="1.0",
               package_hash="fixture-package-hash", content_available=0)
        insert(c, "legacy_verifications", id=1, code_id=1, actor_admin_id=1, audit_event_id=1, purpose="sale",
               order_id=1, issued_policy_json=payload, reason="Fixture provenance", verified_at=NOW, revision=2)
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert c.execute("SELECT product_id, order_id, source_code_id FROM entitlements").fetchone()[:] == (1, 1, 1)
        assert c.execute("SELECT completed FROM entitlement_progress").fetchone()[0] == 1
    graph = read_m1_graph(path)
    assert all(len(rows) == 1 for rows in graph.values())
    return graph


def test_v5_upgrade_preserves_every_m1_row(tmp_path):
    db, backup = tmp_path / "v5.db", tmp_path / "before-v6.db"
    migrate_database(db, through_version=5)
    before = seed_v5_graph(db)
    old_history = history(db)
    assert migrate_database(db, backup_path=backup).to_version == 6
    assert read_m1_graph(db) == read_m1_graph(backup) == before
    assert history(db)[:5] == history(backup) == old_history
    assert [row[0] for row in history(db)] == [1, 2, 3, 4, 5, 6]
    assert check_database(backup)["version"] == 5
    assert check_database(db) == {"version": 6, "integrity": "ok", "foreign_keys": "ok"}
    with closing(open_readonly(db)) as c:
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert PRODUCTION_TABLES <= {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    unchanged = db.read_bytes()
    assert migrate_database(db).to_version == 6
    assert db.read_bytes() == unchanged


def test_v6_failure_rolls_back(tmp_path, monkeypatch):
    from course_platform import migrations

    db, backup = tmp_path / "v5.db", tmp_path / "rollback.db"
    migrate_database(db, through_version=5)
    before = seed_v5_graph(db)
    old_history = history(db)
    with closing(open_readonly(db)) as c:
        schema = [tuple(r) for r in c.execute("SELECT * FROM sqlite_master ORDER BY type, name")]
    assert len(migrations.MIGRATIONS) == 6
    original = migrations.MIGRATIONS[5].apply

    def fail(connection):
        original(connection)
        connection.execute("UPDATE products SET title='partial'")
        raise RuntimeError("injected schema6 failure")

    monkeypatch.setattr(migrations.MIGRATIONS[5], "apply", fail)
    with pytest.raises(RuntimeError, match="injected schema6 failure"):
        migrate_database(db, backup_path=backup)
    assert read_m1_graph(db) == read_m1_graph(backup) == before
    assert history(db) == history(backup) == old_history
    with closing(open_readonly(db)) as c:
        assert [tuple(r) for r in c.execute("SELECT * FROM sqlite_master ORDER BY type, name")] == schema
    assert check_database(db)["version"] == 5


def seed_production(c):
    insert(c, "admins", id=1, username="fixture-owner", password_hash="fake-hash")
    for p in (1, 2):
        insert(c, "production_projects", id=p, actor_admin_id=1, mode="quick")
        insert(c, "brief_revisions", id=p, project_id=p, revision=1, title="Fixture", actor_admin_id=1)
        c.execute("UPDATE production_projects SET current_brief_revision_id=? WHERE id=?", (p, p))
        insert(c, "project_reference_links", project_id=p, reference_id=p)
        insert(c, "reference_revisions", id=p, project_id=p, reference_id=p, revision=1,
               text="Private fixture", content_hash="ref-hash", actor_admin_id=1)
        c.execute("UPDATE project_reference_links SET current_revision_id=? WHERE project_id=?", (p, p))
        insert(c, "production_chapters", id=f"chapter-{p}", project_id=p)
        insert(c, "outline_revisions", id=p, project_id=p, revision=1, item_count=1, input_hash="input-hash",
               source="manual", actor_admin_id=1)
        insert(c, "outline_items", outline_revision_id=p, project_id=p, position=1,
               chapter_id=f"chapter-{p}", title="Fixture", goal="Learn")
        insert(c, "outline_confirmations", id=p, project_id=p, outline_revision_id=p,
               input_hash="input-hash", actor_admin_id=1)
        insert(c, "chapter_revisions", id=p, project_id=p, chapter_id=f"chapter-{p}", revision=1,
               markdown="Private Markdown", source="manual", actor_admin_id=1)
        c.execute("UPDATE production_chapters SET current_revision_id=? WHERE id=?", (p, f"chapter-{p}"))
        insert(c, "production_candidates", id=p, project_id=p, kind="chapter", chapter_id=f"chapter-{p}",
               chapter_revision_id=p, base_body_revision_id=p, input_hash="input-hash")
        insert(c, "draft_checks", id=p, project_id=p, project_revision=1, input_hash="input-hash",
               findings_json="[]", blocking=0, actor_admin_id=1)
        insert(c, "production_snapshots", id=p, project_id=p, project_revision=1, brief_revision_id=p,
               outline_revision_id=p, check_id=p, input_hash="input-hash", snapshot_hash="snapshot-hash", payload_json='{"private":true}',
               actor_admin_id=1, chapter_count=1, reference_count=0)
        insert(c, "production_snapshot_refs", snapshot_id=p, project_id=p, kind="chapter",
               chapter_id=f"chapter-{p}", chapter_revision_id=p)


@pytest.fixture
def production_db(tmp_path):
    path = tmp_path / "production.db"
    migrate_database(path)
    return path


@pytest.mark.parametrize("table", ["brief_revisions", "reference_revisions", "outline_revisions",
    "outline_items", "outline_confirmations", "chapter_revisions", "draft_checks",
    "production_snapshots", "production_snapshot_refs"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_snapshot_and_revision_rows_are_immutable(production_db, table, operation):
    # Even a no-op UPDATE must fail; immutable source rows cannot be rewritten.
    with transaction(production_db) as c:
        seed_production(c)
        column = c.execute(f"PRAGMA table_info({table})").fetchone()[1]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            c.execute(f"UPDATE {table} SET {column}={column}" if operation == "UPDATE" else f"DELETE FROM {table}")


@pytest.mark.parametrize("statement", [
    "UPDATE production_projects SET current_brief_revision_id=2 WHERE id=1",
    "UPDATE production_projects SET current_outline_revision_id=2 WHERE id=1",
    "UPDATE production_projects SET current_confirmation_id=2 WHERE id=1",
    "UPDATE project_reference_links SET current_revision_id=2 WHERE project_id=1",
    "UPDATE production_chapters SET current_revision_id=2 WHERE project_id=1",
    "UPDATE production_candidates SET chapter_revision_id=2 WHERE id=1",
    "UPDATE production_candidates SET project_id=2 WHERE id=1",
    "UPDATE production_chapters SET project_id=2, current_revision_id=NULL WHERE project_id=1",
    "INSERT INTO outline_items (outline_revision_id, project_id, position, chapter_id, title, goal) VALUES (1, 1, 2, 'chapter-2', 'Bad', 'Bad')",
    "INSERT INTO production_snapshot_refs (snapshot_id, project_id, kind, chapter_id, chapter_revision_id) VALUES (1, 1, 'chapter', 'chapter-2', 2)",
    "INSERT INTO production_snapshot_refs (snapshot_id, project_id, kind, reference_revision_id) VALUES (1, 1, 'reference', 2)",
    "UPDATE production_projects SET revision=1.5 WHERE id=1",
])
def test_cross_project_links_and_invalid_revisions_are_rejected(production_db, statement):
    with transaction(production_db) as c:
        seed_production(c)
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(statement)


def add_job(c, job_id, project_id, **extra):
    values = dict(id=job_id, project_id=project_id, actor_admin_id=1,
                  kind="connection_test" if project_id is None else "chapters_generate",
                  input_hash="input-hash", input_json="{}", config_json="{}", request_digest="digest")
    values.update(extra)
    insert(c, "production_jobs", **values)


def test_job_slots_are_global_and_per_project(production_db):
    with transaction(production_db) as c:
        seed_production(c)
        add_job(c, 1, 1)
        with pytest.raises(sqlite3.IntegrityError):
            add_job(c, 2, 1)
        add_job(c, 2, 2)
        c.execute("UPDATE production_jobs SET state='running' WHERE id=1")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE production_jobs SET state='running' WHERE id=2")
        add_job(c, 3, None)
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE production_jobs SET state='running' WHERE id=3")
        c.execute("UPDATE production_jobs SET state='succeeded' WHERE id=1")
        c.execute("UPDATE production_jobs SET state='running' WHERE id=3")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE production_jobs SET state='running' WHERE id=1")


def test_idempotency_and_attempt_ownership_are_enforced(production_db):
    with transaction(production_db) as c:
        seed_production(c)
        add_job(c, 1, 1)
        add_job(c, 2, 2)
        insert(c, "job_requests", actor_admin_id=1, action="submit", idempotency_key="key",
               request_digest="digest", job_id=1)
        with pytest.raises(sqlite3.IntegrityError):
            insert(c, "job_requests", actor_admin_id=1, action="submit", idempotency_key="key",
                   request_digest="different", job_id=2)
        insert(c, "job_attempts", id=1, job_id=1, attempt_number=1, attempt_token="fake-token", worker_id="worker",
               started_at=NOW, heartbeat_at=NOW, lease_until=NOW)
        with pytest.raises(sqlite3.IntegrityError):
            insert(c, "model_calls", job_id=2, attempt_id=1, call_number=1, provider="fixture", model="fixture",
                   prompt_version="fixture", input_hash="hash", reserved_tokens=32, started_at=NOW)
        with pytest.raises(sqlite3.IntegrityError):
            add_job(c, 3, None, kind="chapters_generate")


@pytest.mark.parametrize("table", ["brief_revisions", "reference_revisions", "outline_revisions",
    "outline_items", "outline_confirmations", "chapter_revisions", "draft_checks",
    "production_snapshots", "production_snapshot_refs"])
def test_replace_cannot_rewrite_immutable_sources(production_db, table):
    with transaction(production_db) as c:
        seed_production(c)
        # SQLite REPLACE implicitly deletes conflicting rows, with recursive
        # triggers disabled by default. An UPDATE/DELETE trigger alone is weak.
        assert c.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        # Generated closure columns are read-only and cannot be supplied to
        # INSERT. Name every insertable source column to test REPLACE itself.
        columns = ", ".join(row[1] for row in c.execute(f"PRAGMA table_info({table})"))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            c.execute(f"INSERT OR REPLACE INTO {table} ({columns}) SELECT {columns} FROM {table}")


def test_job_step_rejects_candidate_for_another_chapter_in_same_project(production_db):
    with transaction(production_db) as c:
        seed_production(c)
        insert(c, "production_chapters", id="chapter-other", project_id=1)
        insert(c, "chapter_revisions", id=3, project_id=1, chapter_id="chapter-other", revision=1,
               markdown="Other chapter", source="manual", actor_admin_id=1)
        insert(c, "production_candidates", id=3, project_id=1, kind="chapter", chapter_id="chapter-other",
               chapter_revision_id=3, input_hash="input-hash")
        add_job(c, 1, 1)
        insert(c, "job_attempts", id=1, job_id=1, attempt_number=1, attempt_token="fake-token", worker_id="worker",
               started_at=NOW, heartbeat_at=NOW, lease_until=NOW)
        with pytest.raises(sqlite3.IntegrityError):
            insert(c, "job_steps", job_id=1, attempt_id=1, project_id=1, step_number=1, kind="chapter",
                   chapter_id="chapter-1", input_hash="input-hash", candidate_id=3)


@pytest.mark.parametrize("table", ["production_jobs", "job_attempts", "model_calls"])
def test_update_replace_cannot_evict_occupied_slot_history(production_db, table):
    with transaction(production_db) as c:
        seed_production(c)
        assert c.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        add_job(c, 1, 1)
        if table == "production_jobs":
            add_job(c, 2, 2)
            c.execute("UPDATE production_jobs SET state='running' WHERE id=1")
            statement = "UPDATE OR REPLACE production_jobs SET state='running' WHERE id=2"
        else:
            insert(c, "job_attempts", id=1, job_id=1, attempt_number=1, attempt_token="fake-one", worker_id="worker",
                   started_at=NOW, heartbeat_at=NOW, lease_until=NOW,
                   state="interrupted" if table == "job_attempts" else "running")
            if table == "job_attempts":
                insert(c, "job_attempts", id=2, job_id=1, attempt_number=2, attempt_token="fake-two", worker_id="worker",
                       started_at=NOW, heartbeat_at=NOW, lease_until=NOW)
                statement = "UPDATE OR REPLACE job_attempts SET state='running' WHERE id=1"
            else:
                for call_id, status in ((1, "unknown"), (2, "sent")):
                    insert(c, "model_calls", id=call_id, job_id=1, attempt_id=1, call_number=call_id,
                           provider="fixture", model="fixture", prompt_version="fixture", input_hash="hash",
                           reserved_tokens=32, started_at=NOW, status=status)
                statement = "UPDATE OR REPLACE model_calls SET status='sent' WHERE id=1"
        before = [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY id")]
        assert len(before) == 2
        with pytest.raises(sqlite3.IntegrityError, match="slot"):
            c.execute(statement)
        assert [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY id")] == before
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        # A row keeping its own slot must not be mistaken for a collision.
        column = "status" if table == "model_calls" else "state"
        c.execute(f"UPDATE OR REPLACE {table} SET {column}={column}")
        assert [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY id")] == before


def production_rows(c):
    return {table: [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in sorted(PRODUCTION_TABLES)}


@pytest.mark.parametrize("collection", ["outline", "snapshot"])
def test_late_insert_cannot_extend_frozen_source_collection(production_db, collection):
    with transaction(production_db) as c:
        seed_production(c)
        insert(c, "production_chapters", id="extra-chapter", project_id=1)
        before = production_rows(c)
        with pytest.raises(sqlite3.IntegrityError, match="collection"):
            if collection == "outline":
                insert(c, "outline_items", outline_revision_id=1, project_id=1, position=2,
                       chapter_id="extra-chapter", title="Late chapter", goal="Late goal")
            else:
                insert(c, "production_snapshot_refs", snapshot_id=1, project_id=1, kind="reference",
                       reference_revision_id=1)
        assert production_rows(c) == before
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("binding", ["current", "confirmation", "candidate", "snapshot", "job_step"])
def test_incomplete_outline_cannot_be_bound_before_collection_is_complete(production_db, binding):
    with transaction(production_db) as c:
        seed_production(c)
        insert(c, "outline_revisions", id=3, project_id=1, revision=2, item_count=2,
               input_hash="input-hash", source="manual", actor_admin_id=1)
        insert(c, "outline_items", outline_revision_id=3, project_id=1, position=1,
               chapter_id="chapter-1", title="First", goal="Learn")
        add_job(c, 1, 1)
        insert(c, "job_attempts", id=1, job_id=1, attempt_number=1, attempt_token="fake-token", worker_id="worker",
               started_at=NOW, heartbeat_at=NOW, lease_until=NOW)

        def bind():
            if binding == "current":
                c.execute("UPDATE production_projects SET current_outline_revision_id=3 WHERE id=1")
            elif binding == "confirmation":
                insert(c, "outline_confirmations", id=3, project_id=1, outline_revision_id=3,
                       input_hash="input-hash", actor_admin_id=1)
            elif binding == "candidate":
                insert(c, "production_candidates", id=3, project_id=1, kind="outline",
                       outline_revision_id=3, input_hash="input-hash")
            elif binding == "snapshot":
                insert(c, "production_snapshots", id=3, project_id=1, project_revision=1, brief_revision_id=1,
                       outline_revision_id=3, check_id=1, input_hash="input-hash", snapshot_hash="new-hash",
                       payload_json="{}", actor_admin_id=1, chapter_count=2, reference_count=0)
            else:
                insert(c, "job_steps", job_id=1, attempt_id=1, project_id=1, step_number=1, kind="outline",
                       input_hash="input-hash", outline_revision_id=3)

        before = production_rows(c)
        with pytest.raises(sqlite3.IntegrityError, match="incomplete outline"):
            bind()
        assert production_rows(c) == before
        insert(c, "production_chapters", id="extra-chapter", project_id=1)
        insert(c, "outline_items", outline_revision_id=3, project_id=1, position=2,
               chapter_id="extra-chapter", title="Second", goal="Learn")
        bind()
        if binding == "snapshot":
            insert(c, "chapter_revisions", id=3, project_id=1, chapter_id="extra-chapter", revision=1,
                   markdown="Extra chapter", source="manual", actor_admin_id=1)
            insert(c, "production_snapshot_refs", snapshot_id=3, project_id=1, kind="chapter", position=1,
                   chapter_id="chapter-1", chapter_revision_id=1)
            insert(c, "production_snapshot_refs", snapshot_id=3, project_id=1, kind="chapter", position=2,
                   chapter_id="extra-chapter", chapter_revision_id=3)
        assert c.execute("SELECT COUNT(*) FROM outline_items WHERE outline_revision_id=3").fetchone()[0] == 2


@pytest.mark.parametrize("table,column,value", [
    ("outline_revisions", "item_count", 0), ("outline_revisions", "item_count", 61),
    ("outline_revisions", "item_count", 1.5),
    ("production_snapshots", "chapter_count", 0), ("production_snapshots", "chapter_count", 61),
    ("production_snapshots", "chapter_count", 1.5),
    ("production_snapshots", "reference_count", -1), ("production_snapshots", "reference_count", 11),
    ("production_snapshots", "reference_count", 1.5),
])
def test_invalid_declared_collection_counts_are_rejected(production_db, table, column, value):
    with transaction(production_db) as c:
        seed_production(c)
        values = dict(id=3, project_id=1, actor_admin_id=1, input_hash="input-hash")
        if table == "outline_revisions":
            values.update(revision=2, source="manual", item_count=1)
        else:
            values.update(project_revision=1, brief_revision_id=1, outline_revision_id=1, check_id=1,
                          snapshot_hash="new-hash", payload_json="{}", chapter_count=1, reference_count=0)
        values[column] = value
        before = production_rows(c)
        with pytest.raises(sqlite3.IntegrityError):
            insert(c, table, **values)
        assert production_rows(c) == before


def test_snapshot_collection_allows_declared_assembly_then_rejects_growth(production_db):
    with transaction(production_db) as c:
        seed_production(c)
        insert(c, "production_snapshots", id=3, project_id=1, project_revision=1, brief_revision_id=1,
               outline_revision_id=1, check_id=1, input_hash="input-hash", snapshot_hash="new-hash",
               payload_json="{}", actor_admin_id=1, chapter_count=1, reference_count=1)
        insert(c, "production_snapshot_refs", snapshot_id=3, project_id=1, kind="chapter", chapter_id="chapter-1",
               chapter_revision_id=1)
        insert(c, "production_snapshot_refs", snapshot_id=3, project_id=1, kind="reference", reference_revision_id=1)
        insert(c, "project_reference_links", project_id=1, reference_id=3)
        insert(c, "reference_revisions", id=3, project_id=1, reference_id=3, revision=1,
               text="Extra reference", content_hash="extra-hash", actor_admin_id=1)
        before = production_rows(c)
        with pytest.raises(sqlite3.IntegrityError, match="collection"):
            insert(c, "production_snapshot_refs", snapshot_id=3, project_id=1, kind="reference", position=2,
                   reference_revision_id=3)
        assert production_rows(c) == before
        assert c.execute("SELECT COUNT(*) FROM production_snapshot_refs WHERE snapshot_id=3").fetchone()[0] == 2


@pytest.mark.parametrize("missing", ["chapter", "reference", "reference_first", "reference_middle"])
def test_incomplete_snapshot_cannot_commit_or_gain_children_later(production_db, missing):
    with transaction(production_db) as c:
        seed_production(c)
        for reference_id in (3, 4):
            insert(c, "project_reference_links", project_id=1, reference_id=reference_id)
            insert(c, "reference_revisions", id=reference_id, project_id=1, reference_id=reference_id, revision=1,
                   text="Extra fixture", content_hash="extra-hash", actor_admin_id=1)
        before = production_rows(c)
    counts = {"chapter": 0, "reference": 1, "reference_first": 2, "reference_middle": 3}
    reached_commit = False
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        with transaction(production_db) as c:
            assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            insert(c, "production_snapshots", id=3, project_id=1, project_revision=1, brief_revision_id=1,
                   outline_revision_id=1, check_id=1, input_hash="input-hash", snapshot_hash="new-hash",
                   payload_json="{}", actor_admin_id=1, chapter_count=1, reference_count=counts[missing])
            if missing != "chapter":
                insert(c, "production_snapshot_refs", snapshot_id=3, project_id=1, kind="chapter",
                       chapter_id="chapter-1", chapter_revision_id=1)
            if missing == "reference_first":
                insert(c, "production_snapshot_refs", snapshot_id=3, project_id=1, kind="reference", position=2,
                       reference_revision_id=3)
            elif missing == "reference_middle":
                insert(c, "production_snapshot_refs", snapshot_id=3, project_id=1, kind="reference", position=1,
                       reference_revision_id=1)
                insert(c, "production_snapshot_refs", snapshot_id=3, project_id=1, kind="reference", position=3,
                       reference_revision_id=4)
            reached_commit = True
    # The parent and valid initial children may be assembled; rejection occurs
    # at the actual COMMIT, and the owned transaction rolls everything back.
    assert reached_commit
    with transaction(production_db) as c:
        assert production_rows(c) == before
        assert c.execute("SELECT id FROM production_snapshots WHERE id=3").fetchone() is None
        with pytest.raises(sqlite3.IntegrityError):
            insert(c, "production_snapshot_refs", snapshot_id=3, project_id=1, kind="reference",
                   reference_revision_id=1)
        assert production_rows(c) == before
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("reference_count,child_count", [(0, 1), (2, 3)])
def test_snapshot_children_complete_atomically_in_any_order_and_stay_frozen(production_db, reference_count, child_count):
    with transaction(production_db) as c:
        seed_production(c)
        for reference_id in (3, 4):
            insert(c, "project_reference_links", project_id=1, reference_id=reference_id)
            insert(c, "reference_revisions", id=reference_id, project_id=1, reference_id=reference_id, revision=1,
                   text="Extra fixture", content_hash="extra-hash", actor_admin_id=1)
    with transaction(production_db) as c:
        insert(c, "production_snapshots", id=3, project_id=1, project_revision=1, brief_revision_id=1,
               outline_revision_id=1, check_id=1, input_hash="input-hash", snapshot_hash="new-hash",
               payload_json="{}", actor_admin_id=1, chapter_count=1, reference_count=reference_count)
        if reference_count:
            insert(c, "production_snapshot_refs", snapshot_id=3, project_id=1, kind="reference", position=2,
                   reference_revision_id=3)
            insert(c, "production_snapshot_refs", snapshot_id=3, project_id=1, kind="reference", position=1,
                   reference_revision_id=1)
        insert(c, "production_snapshot_refs", snapshot_id=3, project_id=1, kind="chapter", position=1,
               chapter_id="chapter-1", chapter_revision_id=1)
        before = production_rows(c)
    # In a later, separate transaction the fully committed collection cannot
    # gain another child; its entire parent and child graph remains unchanged.
    with transaction(production_db) as c:
        assert c.execute("SELECT COUNT(*) FROM production_snapshot_refs WHERE snapshot_id=3").fetchone()[0] == child_count
        with pytest.raises(sqlite3.IntegrityError, match="collection"):
            insert(c, "production_snapshot_refs", snapshot_id=3, project_id=1, kind="reference", position=reference_count+1,
                   reference_revision_id=4)
        assert production_rows(c) == before
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
