"""Audit must share success commits and never persist raw credential payloads."""

import json
import sqlite3

import pytest

from course_platform.audit import AuditEvent, append_event, record_denial
from course_platform.database import transaction
from course_platform.domain import BusinessError


def event(**overrides):
    fields = dict(actor_admin_id=None, object_type="code", object_id="1", action="code.redeem",
                  reason="invalid_credential", outcome="denied", request_id="request-1",
                  changes={"error_code": "invalid_credential"})
    fields.update(overrides)
    return AuditEvent(**fields)


def test_business_and_success_audit_roll_back_together(db_path):
    with pytest.raises(RuntimeError):
        with transaction(db_path) as connection:
            connection.execute("INSERT INTO categories (slug, name) VALUES ('ai', 'AI')")
            append_event(connection, event(action="category.create", outcome="success", reason="created", changes={"revision": 1}))
            raise RuntimeError("business failure")
    with transaction(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM admin_events").fetchone()[0] == 0


def test_success_audit_commits_with_business_write(db_path, clock):
    with transaction(db_path) as connection:
        connection.execute("INSERT INTO categories (slug, name) VALUES ('ai', 'AI')")
        append_event(connection, event(action="category.create", outcome="success", reason="created", changes={"revision": 1}))
    with transaction(db_path) as connection:
        row = connection.execute("SELECT * FROM admin_events").fetchone()
        assert row["outcome"] == "success"
        assert row["request_id"] == "request-1"
        assert json.loads(row["changes_json"]) == {"revision": 1}
        assert connection.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 1
    assert clock.now().isoformat() == "2026-10-02T00:00:00+00:00"
    clock.advance(minutes=10)
    assert clock.now().isoformat() == "2026-10-02T00:10:00+00:00"


def test_denial_survives_failed_business_transaction(db_path):
    try:
        with transaction(db_path) as connection:
            connection.execute("INSERT INTO categories (slug, name) VALUES ('ai', 'AI')")
            raise BusinessError("invalid_credential", "Credential rejected", 403)
    except BusinessError:
        record_denial(db_path, event())
    with transaction(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0
        rows = connection.execute("SELECT * FROM admin_events").fetchall()
        assert len(rows) == 1 and rows[0]["outcome"] == "denied"
        assert json.loads(rows[0]["changes_json"]) == {"error_code": "invalid_credential"}


@pytest.mark.parametrize("changes", [
    {"code": "CS-secret"}, {"password": "secret"}, {"raw_request": {"token": "secret"}},
    {"error_code": "LK-secret"}, {"error_code": {"token": "secret"}},
    {"error_code": "arbitrary raw request"}, {"quantity": "CS-secret"},
])
def test_audit_rejects_non_allowlisted_or_unsafe_changes(db_path, changes):
    with pytest.raises(BusinessError):
        record_denial(db_path, event(changes=changes))
    with transaction(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM admin_events").fetchone()[0] == 0


@pytest.mark.parametrize("overrides", [
    {"action": "unregistered.action"}, {"outcome": "unknown"},
    {"reason": "CS-secret"}, {"object_id": "LK-secret"}, {"request_id": "CS-secret"},
])
def test_audit_rejects_unsafe_event_envelope(db_path, overrides):
    with pytest.raises(BusinessError):
        record_denial(db_path, event(**overrides))


def test_append_event_requires_caller_transaction(db_path):
    from course_platform.database import connect

    connection = connect(db_path)
    try:
        with pytest.raises(BusinessError):
            append_event(connection, event(outcome="success"))
    finally:
        connection.close()


def test_record_denial_cannot_commit_success(db_path):
    with pytest.raises(BusinessError):
        record_denial(db_path, event(outcome="success"))


def test_audit_uses_admin_foreign_key_and_trusted_actor(db_path, actor):
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(db_path) as connection:
            append_event(connection, event(actor_admin_id=actor.admin_id, request_id=actor.request_id))
    with transaction(db_path) as connection:
        connection.execute("INSERT INTO admins (id, username, password_hash) VALUES (1, 'owner', 'argon2-hash')")
    record_denial(db_path, event(actor_admin_id=actor.admin_id, request_id=actor.request_id))
    with transaction(db_path) as connection:
        assert connection.execute("SELECT actor_admin_id FROM admin_events").fetchone()[0] == 1
