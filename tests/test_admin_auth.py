"""Owner authentication against real migrated SQLite and Argon2 hashes."""

import hashlib

import pytest

from course_platform.admin.auth import AdminService
from course_platform.database import transaction
from course_platform.domain import Actor, BusinessError


PASSWORD = "example-pass-123"


def login(service, **kwargs):
    return service.login("owner", PASSWORD, source="source-hash", request_id="login-test", **kwargs)


def test_password_change_revokes_all_admin_sessions(admin_service, actor):
    admin_service.initialize_owner("owner", "a" * 12)
    grants = [admin_service.login("owner", "a" * 12, source="source", request_id="r") for _ in range(2)]
    admin_service.change_password(actor, "a" * 12, "b" * 12)
    for grant in grants:
        with pytest.raises(BusinessError):
            admin_service.require_session(grant.token, request_id="after")
    with pytest.raises(BusinessError):
        admin_service.login("owner", "a" * 12, source="source", request_id="old")
    grant = admin_service.login("owner", "b" * 12, source="source", request_id="new")
    assert admin_service.require_session(grant.token, request_id="new").admin_id == actor.admin_id


@pytest.mark.parametrize("length,accepted", [(11, False), (12, True), (128, True), (129, False)])
def test_initialize_password_boundaries(admin_service, length, accepted):
    if accepted:
        assert admin_service.initialize_owner("owner", "a" * length) == 1
    else:
        with pytest.raises(BusinessError) as error:
            admin_service.initialize_owner("owner", "a" * length)
        assert error.value.status_code == 400


@pytest.mark.parametrize("length,accepted", [(11, False), (12, True), (128, True), (129, False)])
def test_password_change_boundaries(admin_service, actor, length, accepted):
    admin_service.initialize_owner("owner", PASSWORD)
    grant = login(admin_service)
    if accepted:
        admin_service.change_password(actor, PASSWORD, "b" * length)
        with pytest.raises(BusinessError):
            admin_service.require_session(grant.token, request_id="after")
    else:
        with pytest.raises(BusinessError):
            admin_service.change_password(actor, PASSWORD, "b" * length)
        assert admin_service.require_session(grant.token, request_id="after").admin_id == 1


def test_single_owner_normalization_and_hash_only_storage(admin_service, db_path):
    assert admin_service.initialize_owner("  OwNeR  ", PASSWORD) == 1
    grant = login(admin_service)
    with pytest.raises(BusinessError) as error:
        admin_service.initialize_owner("other", PASSWORD)
    assert error.value.status_code == 409
    with transaction(db_path) as connection:
        owner = dict(connection.execute("SELECT * FROM admins").fetchone())
        session = dict(connection.execute("SELECT * FROM admin_sessions").fetchone())
        events = [dict(row) for row in connection.execute("SELECT * FROM admin_events")]
    assert owner["username"] == "owner"
    assert owner["role"] == "owner"
    assert owner["password_hash"].startswith("$argon2id$")
    assert session["token_hash"] == hashlib.sha256(grant.token.encode()).hexdigest()
    assert session["csrf_hash"] == hashlib.sha256(grant.csrf_token.encode()).hexdigest()
    assert session["created_at"] == "2026-10-02T00:00:00+00:00"
    assert session["expires_at"] == "2026-10-02T08:00:00+00:00"
    for secret in (PASSWORD, grant.token, grant.csrf_token):
        assert secret not in str(owner) + str(session) + str(events)


def test_admin_idle_timeout(admin_service, clock):
    admin_service.initialize_owner("owner", PASSWORD)
    grant = login(admin_service)
    clock.advance(minutes=30)
    with pytest.raises(BusinessError):
        admin_service.require_session(grant.token, request_id="r2")


def test_admin_absolute_and_idle_expiry(admin_service, clock):
    admin_service.initialize_owner("owner", PASSWORD)
    grant = login(admin_service)
    for _ in range(16):
        clock.advance(minutes=29)
        admin_service.require_session(grant.token, request_id="activity")
    clock.advance(minutes=16)
    with pytest.raises(BusinessError):
        admin_service.require_session(grant.token, request_id="absolute")
    grant = login(admin_service)
    clock.advance(minutes=29, seconds=59)
    admin_service.require_session(grant.token, request_id="refresh")
    clock.advance(minutes=30)
    with pytest.raises(BusinessError):
        admin_service.require_session(grant.token, request_id="idle")


def test_login_rotation_keeps_other_devices_and_failure_keeps_old_session(admin_service):
    admin_service.initialize_owner("owner", PASSWORD)
    original, other = login(admin_service), login(admin_service)
    with pytest.raises(BusinessError):
        admin_service.login("owner", "wrong-password", source="source", request_id="failure",
                            previous_token=original.token)
    assert admin_service.require_session(original.token, request_id="still-valid").admin_id == 1
    replacement = login(admin_service, previous_token=original.token)
    assert len({original.token, other.token, replacement.token}) == 3
    with pytest.raises(BusinessError):
        admin_service.require_session(original.token, request_id="revoked")
    assert admin_service.require_session(other.token, request_id="other-device").admin_id == 1


def test_failed_password_change_retains_sessions_and_audits_safely(admin_service, actor, db_path):
    admin_service.initialize_owner("owner", PASSWORD)
    grant = login(admin_service)
    with pytest.raises(BusinessError):
        admin_service.change_password(actor, "wrong-secret", "new-password-123")
    assert admin_service.require_session(grant.token, request_id="still-valid").admin_id == 1
    with transaction(db_path) as connection:
        events = [dict(row) for row in connection.execute("SELECT * FROM admin_events")]
        assert connection.execute("SELECT revision FROM admins").fetchone()[0] == 1
    assert events[-1]["action"] == "admin.password_change"
    assert events[-1]["outcome"] == "denied"
    assert "wrong-secret" not in str(events) and "new-password-123" not in str(events)


def test_login_failure_limit_survives_restart_and_audits_without_secrets(admin_service, db_path, clock):
    admin_service.initialize_owner("owner", PASSWORD)
    for _ in range(5):
        with pytest.raises(BusinessError) as error:
            admin_service.login(" OWNER ", "wrong-secret", source="source", request_id="failure")
        assert error.value.status_code == 401
    with pytest.raises(BusinessError) as error:
        AdminService(db_path, clock=clock.now).login("owner", PASSWORD, source="fresh", request_id="limit")
    assert error.value.status_code == 429
    assert error.value.headers == {"Retry-After": "900"}
    with transaction(db_path) as connection:
        events = [dict(row) for row in connection.execute("SELECT * FROM admin_events WHERE outcome='denied'")]
    assert len([row for row in events if row["action"] == "auth.login"]) == 6
    assert "wrong-secret" not in str(events) and PASSWORD not in str(events)
    clock.advance(minutes=15)
    assert login(admin_service).token


def test_disabled_owner_and_logout_cannot_authorize(admin_service, db_path, actor):
    admin_service.initialize_owner("owner", PASSWORD)
    grant = login(admin_service)
    with pytest.raises(BusinessError):
        admin_service.logout(grant.token, Actor(99, "wrong-actor"))
    admin_service.logout(grant.token, actor)
    with pytest.raises(BusinessError):
        admin_service.require_session(grant.token, request_id="logged-out")
    grant = login(admin_service)
    with transaction(db_path, immediate=True) as connection:
        connection.execute("UPDATE admins SET enabled=0")
    with pytest.raises(BusinessError):
        login(admin_service)
    with pytest.raises(BusinessError):
        admin_service.require_session(grant.token, request_id="disabled")


def test_unmigrated_service_does_not_create_database(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(BusinessError) as error:
        AdminService(path).initialize_owner("owner", PASSWORD)
    assert error.value.status_code == 503
    assert not path.exists()


def test_stale_password_revision_rejects_without_revocation(admin_service, actor):
    admin_service.initialize_owner("owner", PASSWORD)
    grant = login(admin_service)
    with pytest.raises(BusinessError) as error:
        admin_service.change_password(actor, PASSWORD, "replacement-pass", expected_revision=0)
    assert error.value.status_code == 409
    assert admin_service.require_session(grant.token, request_id="after").admin_id == 1


@pytest.mark.parametrize("operation,action", [("initialize", "auth.initialize"), ("logout", "auth.logout")])
def test_auth_write_denials_are_audited_after_rollback(admin_service, actor, db_path, operation, action):
    admin_service.initialize_owner("owner", PASSWORD)
    grant = login(admin_service)
    with pytest.raises(BusinessError):
        if operation == "initialize":
            admin_service.initialize_owner("other", PASSWORD)
        else:
            admin_service.logout(grant.token, Actor(99, "denied-logout"))
    assert admin_service.require_session(grant.token, request_id="unchanged").admin_id == 1
    with transaction(db_path) as connection:
        event = connection.execute("SELECT * FROM admin_events ORDER BY id DESC LIMIT 1").fetchone()
    assert event["action"] == action
    assert event["outcome"] == "denied"
    assert PASSWORD not in str(dict(event)) and grant.token not in str(dict(event))
