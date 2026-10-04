"""Real original-program migration and owner support, never verification flags."""

import hashlib
import json
from contextlib import closing
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_migrations import populated_v2  # noqa: F401

from course_platform.access import AccessService
from course_platform.admin.auth import AdminService
from course_platform.content import CourseManifest
from course_platform.database import (backup_database, check_database, connect,
    initialize_database, migrate_database, sync_course, to_db_time, transaction)
from course_platform.delivery.entitlements import EntitlementService
from course_platform.delivery.progress import ProgressService
from course_platform.delivery.recovery import RecoveryService
from course_platform.domain import Actor, BusinessError
from course_platform.operations.products import (AccessPolicy, ProductService,
    SalesChannel, SalesChecklist)


def rows(path, sql, args=()):
    with closing(connect(path)) as connection:
        return [dict(row) for row in connection.execute(sql, args)]


@pytest.fixture
def original(tmp_path, fixture_package, clock, monkeypatch):
    # Real old producer, with only its clock controlled. Dropping conversion
    # or combining sessions must break independent history/progress assertions.
    monkeypatch.setattr("course_platform.access.utc_now", clock.now)
    path = tmp_path / "original.db"
    initialize_database(path)
    manifest = CourseManifest.model_validate_json((fixture_package / "manifest.json").read_bytes())
    sync_course(manifest, fixture_package, path)
    access = AccessService(path)
    used = access.create_access_code(manifest.course_id, clock.now() + timedelta(days=20))
    session = access.redeem_access_code(used, manifest.course_id)
    access.record_progress(session.session_id, manifest.course_id, 1, True)
    second = access.redeem_access_code(access.create_access_code(manifest.course_id), manifest.course_id)
    access.record_progress(second.session_id, manifest.course_id, 1, False)
    third = access.redeem_access_code(access.create_access_code(manifest.course_id), manifest.course_id)
    access.record_progress(third.session_id, manifest.course_id, 1, True)
    with transaction(path) as connection:
        connection.execute("UPDATE sessions SET expires_at=? WHERE session_hash=?",
            (to_db_time(clock.now() - timedelta(days=1)), hashlib.sha256(third.session_id.encode()).hexdigest()))
    unused = access.create_access_code(manifest.course_id, clock.now() + timedelta(days=10))
    no_deadline = access.create_access_code(manifest.course_id)
    copied = tmp_path / "copy.db"
    backup_database(path, copied)
    return SimpleNamespace(source=path, db=copied, package=fixture_package, course=manifest.course_id,
        unused=unused, used=used, no_deadline=no_deadline, sessions=(session, second, third))


def upgrade(original, tmp_path):
    migrate_database(original.db, backup_path=tmp_path / "before-v5.db")


def owner_setup(original, clock):
    from course_platform.operations.legacy import LegacyService
    admin = AdminService(original.db, clock=clock.now)
    admin.initialize_owner("owner", "example-pass-123")
    actor = Actor(1, "legacy-owner")
    return actor, LegacyService(original.db, clock=clock.now)


def activate(original, actor, clock):
    products = ProductService(original.db, clock=clock.now)
    draft = products.get_product(rows(original.db, "SELECT id FROM products")[0]["id"])
    data = draft.data.model_copy(update=dict(synopsis="Course", audience="Learners", prerequisites="None",
        outcomes=["Practice"], ai_disclosure="AI assisted", support_text="Support",
        channels=[SalesChannel(name="Shop", url="https://shop.example/course")],
        policy=AccessPolicy(access_mode="days", access_days=30, online=True, pdf=False, zip=False,
            update_policy="current_version")))
    updated = products.update(actor, draft.id, draft.revision, data)
    return products.activate(actor, updated.id, updated.revision,
        SalesChecklist(quality=True, sources=True, ai=True, mobile=True, downloads=True))


def services(original, clock):
    rights = EntitlementService(original.db, clock=clock.now)
    return rights, RecoveryService(original.db, clock=clock.now, entitlement_service=rights), \
        ProgressService(original.db, clock=clock.now, entitlement_service=rights)


def test_legacy_sessions_are_never_merged_or_extended(original, tmp_path, clock):
    before = rows(original.source, "SELECT * FROM sessions")
    old_progress = rows(original.source, "SELECT * FROM progress")
    upgrade(original, tmp_path)
    after = rows(original.db, "SELECT * FROM sessions")
    assert len({row["entitlement_id"] for row in after}) == 3
    for old, new in zip(before, after, strict=True):
        assert all(new[key] == value for key, value in old.items())
        assert new["source_code_id"] is None and new["csrf_hash"] is None
        right = rows(original.db, "SELECT * FROM entitlements WHERE id=?", (new["entitlement_id"],))[0]
        assert right["expires_at"] == old["expires_at"]
        assert right["legacy_state"] == "pending_verification"
        assert right["source_code_id"] is None and right["issued_policy_json"] is None
        assert right["created_by"] is None
        assert rows(original.db, "SELECT chapter_number, completed, updated_at FROM entitlement_progress WHERE entitlement_id=?",
            (right["id"],)) == [{key: value for key, value in progress.items() if key in ("chapter_number", "completed", "updated_at")}
                for progress in old_progress if progress["session_hash"] == old["session_hash"]]
    assert rows(original.db, "SELECT * FROM progress") == old_progress
    assert rows(original.db, "SELECT * FROM recovery_credentials") == []
    assert rows(original.db, "SELECT created_by, access_days, update_policy, status FROM products") == [
        dict(created_by=None, access_days=None, update_policy=None, status="draft")]
    assert rows(original.db, "SELECT count(*) AS n FROM admins") == [dict(n=0)]
    assert check_database(original.db)["version"] == 5
    rights, _, progress = services(original, clock)
    assert progress.get_progress(original.sessions[0].session_id, original.course) == {1: True}
    with pytest.raises(BusinessError):
        rights.redeem(original.unused, expected_course_id=original.course, request_id="unverified")
    unchanged = original.db.read_bytes()
    assert migrate_database(original.db).to_version == 5
    assert original.db.read_bytes() == unchanged


@pytest.mark.parametrize("purpose", ["test", "gift", "sale"])
def test_expired_legacy_progress_requires_verified_support(original, tmp_path, clock, purpose):
    upgrade(original, tmp_path)
    actor, legacy = owner_setup(original, clock)
    product = activate(original, actor, clock)
    rights, recovery, progress = services(original, clock)
    entitlement_id = rows(original.db, "SELECT entitlement_id FROM sessions ORDER BY expires_at")[0]["entitlement_id"]
    with pytest.raises(BusinessError):
        recovery.reset(actor, entitlement_id, 1, "Holder checked", "before-verify")
    order_id = None
    if purpose == "sale":
        from course_platform.operations.orders import OrderInput, OrderService
        order_id = OrderService(original.db, clock=clock.now).record(actor, OrderInput(channel="shop",
            shop_id="shop", external_order_id="verified-001", product_id=product.id, paid_cents=100,
            paid_at=clock.now(), note="Checked external purchase"), "record").id
    expiry = clock.now() + timedelta(days=7)
    assert legacy.verify_entitlement(actor, entitlement_id, 1, order_id, product.data.policy,
        expiry, purpose, "Checked original holder and explicit new deadline", "verify") is None
    assert rows(original.db, "SELECT * FROM recovery_credentials") == []
    assert rows(original.db, "SELECT source_code_id, created_by FROM entitlements WHERE id=?", (entitlement_id,)) == [
        dict(source_code_id=None, created_by=None)]
    # Same normalized request replays before the changed revision; no key minted.
    legacy.verify_entitlement(actor, entitlement_id, 1, order_id, product.data.policy,
        expiry, purpose, " Checked original holder and explicit new deadline ", "verify")
    with pytest.raises(BusinessError) as error:
        legacy.verify_entitlement(actor, entitlement_id, 1, order_id, product.data.policy,
            expiry + timedelta(days=1), purpose, "Checked original holder and explicit new deadline", "verify")
    assert error.value.code == "idempotency_conflict"
    key = recovery.reset(actor, entitlement_id, 2, "Verified holder lost access", "reset").raw_key
    grant = recovery.restore(key, request_id="restore")
    assert progress.get_progress(grant.session_id, original.course) == {1: True}
    assert grant.session_expires_at == clock.now() + timedelta(hours=72)
    progress.set_completed(grant.session_id, original.course, 1, False)
    assert progress.get_progress(grant.session_id, original.course) == {1: False}
    assert len(rows(original.db, "SELECT * FROM progress")) == 3
    assert rows(original.db, "SELECT expires_at FROM entitlements WHERE id=?", (entitlement_id,)) == [dict(expires_at=to_db_time(expiry))]
    new_key = recovery.reset(actor, entitlement_id, 3, "Verified again", "reset-again").raw_key
    with pytest.raises(BusinessError):
        recovery.restore(key, request_id="old-key")
    with pytest.raises(BusinessError):
        rights.require_session(grant.session_id, original.course)
    assert progress.get_progress(recovery.restore(new_key, request_id="new-key").session_id, original.course) == {1: False}
    for secret in (key, new_key, grant.session_id, original.unused):
        assert secret.encode() not in original.db.read_bytes()


def test_original_short_code_requires_real_resolve_then_redeems(original, tmp_path, clock):
    upgrade(original, tmp_path)
    actor, legacy = owner_setup(original, clock)
    product = activate(original, actor, clock)
    rights, recovery, progress = services(original, clock)
    old = rows(original.db, "SELECT * FROM access_codes WHERE code_hash=?", (hashlib.sha256(original.unused.encode()).hexdigest(),))[0]
    assert len(original.unused) == 19
    assert legacy.resolve_code(actor, old["id"], 1, product.data.policy, "gift", "Checked original recipient") is None
    resolved = rows(original.db, "SELECT * FROM access_codes WHERE id=?", (old["id"],))[0]
    for field in ("code_hash", "expires_at", "created_at", "used_at", "batch_id", "created_by"):
        assert resolved[field] == old[field]
    receipt = rights.redeem(original.unused, expected_course_id=original.course, request_id="short")
    assert receipt.entitlement_expires_at == clock.now() + timedelta(days=30)
    progress.set_completed(receipt.session.session_id, original.course, 1, True)
    assert progress.get_progress(recovery.restore(receipt.raw_recovery_key, request_id="restore-short").session_id, original.course) == {1: True}
    reset = recovery.reset(actor, receipt.session.entitlement_id, 1, "Checked original recipient", "reset-short")
    assert progress.get_progress(recovery.restore(reset.raw_key, request_id="reset-short-restore").session_id, original.course) == {1: True}


@pytest.mark.parametrize("fault", ["unverified", "used", "expired", "wrong_course", "missing_origin", "fake_flags"])
def test_short_code_denials_are_generic(original, tmp_path, clock, fault):
    upgrade(original, tmp_path)
    actor, legacy = owner_setup(original, clock)
    product = activate(original, actor, clock)
    code = rows(original.db, "SELECT id FROM access_codes WHERE code_hash=?", (hashlib.sha256(original.unused.encode()).hexdigest(),))[0]
    if fault != "unverified":
        legacy.resolve_code(actor, code["id"], 1, product.data.policy, "test", "Checked holder")
    with transaction(original.db) as connection:
        if fault == "used":
            connection.execute("UPDATE access_codes SET used_at=? WHERE id=?", (to_db_time(clock.now()), code["id"]))
        elif fault == "expired":
            clock.advance(days=11)
        elif fault == "missing_origin":
            connection.execute("DELETE FROM legacy_verifications WHERE code_id=?", (code["id"],))
            connection.execute("DELETE FROM legacy_code_origins WHERE code_id=?", (code["id"],))
        elif fault == "fake_flags":
            connection.execute("DELETE FROM legacy_verifications WHERE code_id=?", (code["id"],))
    with pytest.raises(BusinessError) as error:
        services(original, clock)[0].redeem(original.unused,
            expected_course_id="wrong" if fault == "wrong_course" else original.course, request_id="denial")
    assert (error.value.code, error.value.status_code) == ("redemption_denied", 403)
    assert len(rows(original.db, "SELECT * FROM entitlements")) == 3


@pytest.mark.parametrize("state", ["missing", "unsafe", "identity"])
def test_missing_course_keeps_history_but_blocks_delivery(original, tmp_path, clock, state):
    if state == "missing":
        with transaction(original.db) as connection:
            connection.execute("UPDATE courses SET content_path=?", (str(tmp_path / "missing"),))
    elif state == "unsafe":
        (original.package / "chapters/01.html").write_text("<script>alert(1)</script>", encoding="utf8")
    else:
        with transaction(original.db) as connection:
            connection.execute("UPDATE courses SET version='other-release'")
    upgrade(original, tmp_path)
    assert len(rows(original.db, "SELECT * FROM entitlements")) == 3
    assert len(rows(original.db, "SELECT * FROM entitlement_progress")) == 3
    assert rows(original.db, "SELECT DISTINCT content_available FROM legacy_entitlement_origins") == [dict(content_available=0)]
    actor, legacy = owner_setup(original, clock)
    policy = AccessPolicy(access_mode="days", access_days=7, online=True, pdf=False, zip=False, update_policy="current_version")
    with pytest.raises(BusinessError):
        legacy.verify_entitlement(actor, 1, 1, None, policy, clock.now()+timedelta(days=7), "test", "Checked holder", "verify")
    with pytest.raises(BusinessError):
        services(original, clock)[0].require_session(original.sessions[0].session_id, original.course)


def test_legacy_migration_failure_can_restore_backup(original, tmp_path, monkeypatch):
    from course_platform.migrations import v005_legacy_delivery
    original_apply = v005_legacy_delivery.apply
    before = original.db.read_bytes()
    def fail(connection):
        original_apply(connection)
        raise RuntimeError("legacy fault")
    monkeypatch.setattr(v005_legacy_delivery, "apply", fail)
    with pytest.raises(RuntimeError, match="legacy fault"):
        upgrade(original, tmp_path)
    assert check_database(original.db)["version"] == 0
    assert original.db.read_bytes() == before
    restored = tmp_path / "restored.db"
    backup_database(tmp_path / "before-v5.db", restored)
    monkeypatch.setattr(v005_legacy_delivery, "apply", original_apply)
    migrate_database(restored, backup_path=tmp_path / "restored-before.db")
    assert check_database(restored) == dict(version=5, integrity="ok", foreign_keys="ok")
    assert len(rows(restored, "SELECT * FROM entitlement_progress")) == 3


def test_installed_latest_owner_and_offline_command(original, tmp_path, clock, monkeypatch, capsys):
    from course_platform import cli
    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(database_path=original.db))
    assert cli.main(["migrate", "--backup", str(tmp_path / "cli-backup.db")]) == 0
    assert json.loads(capsys.readouterr().out)["to_version"] == 5
    owner_setup(original, clock)
    assert cli.main(["migrate", "--check-only"]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == 5


@pytest.mark.parametrize("choice", [None, 0, 366, True, 1.5, 7])
def test_no_activation_deadline_requires_explicit_bounded_choice(original, tmp_path, clock, choice):
    upgrade(original, tmp_path)
    actor, legacy = owner_setup(original, clock)
    product = activate(original, actor, clock)
    identity = rows(original.db, "SELECT id FROM access_codes WHERE code_hash=?", (hashlib.sha256(original.no_deadline.encode()).hexdigest(),))[0]["id"]
    if choice != 7:
        with pytest.raises(BusinessError):
            legacy.resolve_code(actor, identity, 1, product.data.policy, "test", "Holder confirmed", activation_days=choice)
        assert rows(original.db, "SELECT expires_at, revision FROM access_codes WHERE id=?", (identity,)) == [dict(expires_at=None, revision=1)]
    else:
        legacy.resolve_code(actor, identity, 1, product.data.policy, "test", "Holder confirmed", activation_days=choice)
        assert rows(original.db, "SELECT expires_at FROM access_codes WHERE id=?", (identity,)) == [dict(expires_at="2026-10-09T00:00:00+00:00")]
        assert services(original, clock)[0].redeem(original.no_deadline, expected_course_id=None, request_id="no-deadline").raw_recovery_key


def test_used_code_resolve_never_assigns_session_or_policy(original, tmp_path, clock):
    upgrade(original, tmp_path)
    actor, legacy = owner_setup(original, clock)
    before = rows(original.db, "SELECT * FROM entitlements")
    identity = rows(original.db, "SELECT id FROM access_codes WHERE code_hash=?", (hashlib.sha256(original.used.encode()).hexdigest(),))[0]["id"]
    legacy.resolve_code(actor, identity, 1, None, "sale", "Verified original sale purpose only")
    assert rows(original.db, "SELECT * FROM entitlements") == before
    assert rows(original.db, "SELECT batch_id, order_id, issued_policy_json FROM access_codes WHERE id=?", (identity,)) == [
        dict(batch_id=None, order_id=None, issued_policy_json=None)]
    with pytest.raises(BusinessError):
        services(original, clock)[0].redeem(original.used, expected_course_id=None, request_id="used")


@pytest.mark.parametrize("fault", ["disabled_owner", "missing_owner", "revision", "empty_reason", "secret_reason", "hash_reason", "no_policy", "missing_expiry", "naive_expiry", "past_expiry", "no_formats", "sale_no_order"])
def test_verification_rejects_incomplete_or_untrusted_evidence(original, tmp_path, clock, fault):
    upgrade(original, tmp_path)
    actor, legacy = owner_setup(original, clock)
    product = activate(original, actor, clock)
    policy, expiry, purpose, reason, revision = product.data.policy, clock.now()+timedelta(days=7), "test", "Checked holder", 1
    if fault == "disabled_owner":
        with transaction(original.db) as connection:
            connection.execute("UPDATE admins SET enabled=0")
    elif fault == "missing_owner":
        actor = Actor(999, "missing-owner")
    elif fault == "revision":
        revision = 2
    elif fault == "empty_reason":
        reason = " "
    elif fault == "secret_reason":
        reason = original.unused
    elif fault == "hash_reason":
        reason = hashlib.sha256(original.unused.encode()).hexdigest()
    elif fault == "no_policy":
        policy = None
    elif fault == "missing_expiry":
        expiry = None
    elif fault == "naive_expiry":
        expiry = expiry.replace(tzinfo=None)
    elif fault == "past_expiry":
        expiry = clock.now()
    elif fault == "no_formats":
        policy = policy.model_copy(update={"online": False})
    else:
        purpose = "sale"
    with pytest.raises(BusinessError):
        legacy.verify_entitlement(actor, 1, revision, None, policy, expiry, purpose, reason, "invalid")
    assert rows(original.db, "SELECT revision, verified_at FROM entitlements WHERE id=1") == [dict(revision=1, verified_at=None)]
    assert rows(original.db, "SELECT * FROM recovery_credentials") == []
    assert rows(original.db, "SELECT * FROM legacy_verifications") == []


@pytest.mark.parametrize("fault", ["delete_proof", "delete_audit", "reason", "purpose", "time", "expiry", "origin", "policy", "cleared_state"])
def test_state_flip_or_modified_proof_never_authorizes_reset_restore(original, tmp_path, clock, fault):
    upgrade(original, tmp_path)
    actor, legacy = owner_setup(original, clock)
    product = activate(original, actor, clock)
    rights, recovery, _ = services(original, clock)
    legacy.verify_entitlement(actor, 1, 1, None, product.data.policy, clock.now()+timedelta(days=7), "gift", "Holder confirmed", "verify")
    key = recovery.reset(actor, 1, 2, "Holder lost credential", "reset").raw_key
    with transaction(original.db) as connection:
        if fault == "delete_proof":
            connection.execute("DELETE FROM legacy_verifications")
        elif fault == "delete_audit":
            connection.execute("UPDATE admin_events SET outcome='denied' WHERE action='legacy.verify'")
        elif fault == "reason":
            connection.execute("UPDATE entitlements SET verified_reason='Other person' WHERE id=1")
        elif fault == "purpose":
            connection.execute("UPDATE entitlements SET purpose='sale' WHERE id=1")
        elif fault == "time":
            connection.execute("UPDATE entitlements SET verified_at='2027-10-02T00:00:00+00:00' WHERE id=1")
            connection.execute("UPDATE legacy_verifications SET verified_at='2027-10-02T00:00:00+00:00'")
        elif fault == "expiry":
            connection.execute("UPDATE entitlements SET expires_at='2027-10-02T00:00:00+00:00' WHERE id=1")
        elif fault == "origin":
            connection.execute("DELETE FROM legacy_entitlement_origins WHERE entitlement_id=1")
        elif fault == "cleared_state":
            connection.execute("DELETE FROM legacy_verifications")
            connection.execute("UPDATE entitlements SET legacy_state=NULL WHERE id=1")
        else:
            connection.execute("UPDATE entitlements SET issued_policy_json=NULL WHERE id=1")
    with pytest.raises(BusinessError):
        recovery.reset(actor, 1, 3, "State alone is not proof", "forged-reset")
    with pytest.raises(BusinessError) as error:
        recovery.restore(key, request_id="forged-restore")
    assert error.value.code == "recovery_denied"


@pytest.mark.parametrize("kind", ["code", "entitlement"])
def test_verification_audit_fault_rolls_back_all_writes(original, tmp_path, clock, kind):
    upgrade(original, tmp_path)
    actor, legacy = owner_setup(original, clock)
    product = activate(original, actor, clock)
    with transaction(original.db) as connection:
        connection.execute("""CREATE TRIGGER fail_legacy_audit BEFORE INSERT ON admin_events
            WHEN NEW.action='legacy.verify' AND NEW.outcome='success'
            BEGIN SELECT RAISE(ABORT, 'private fault'); END""")
    before = rows(original.db, "SELECT * FROM entitlements")
    with pytest.raises(BusinessError) as error:
        if kind == "code":
            identity = rows(original.db, "SELECT id FROM access_codes WHERE code_hash=?", (hashlib.sha256(original.unused.encode()).hexdigest(),))[0]["id"]
            legacy.resolve_code(actor, identity, 1, product.data.policy, "gift", "Holder checked")
        else:
            legacy.verify_entitlement(actor, 1, 1, None, product.data.policy, clock.now()+timedelta(days=7), "gift", "Holder checked", "verify")
    assert "private fault" not in error.value.message
    assert rows(original.db, "SELECT * FROM entitlements") == before
    assert rows(original.db, "SELECT * FROM legacy_verifications") == []
    assert rows(original.db, "SELECT * FROM operation_requests WHERE action='legacy.verify'") == []
    assert len(rows(original.db, "SELECT * FROM admin_events WHERE action='legacy.verify' AND outcome='denied'")) == 1


def test_modern_issuance_with_short_or_malformed_hash_is_not_legacy(original, tmp_path, clock):
    from course_platform.operations.codes import BatchInput, CodeService
    upgrade(original, tmp_path)
    actor, _ = owner_setup(original, clock)
    product = activate(original, actor, clock)
    codes = CodeService(original.db, clock=clock.now)
    for raw in ("CS-" + "x"*16, "CS-" + "x"*31, "CS-" + "x"*33, "arbitrary-token"):
        issued = codes.issue_batch(actor, BatchInput(product_id=product.id, purpose="gift"), "modern-"+str(len(raw))).codes[0]
        assert len(issued.raw_code) == 35
        with transaction(original.db) as connection:
            connection.execute("UPDATE access_codes SET code_hash=? WHERE code_number=?", (hashlib.sha256(raw.encode()).hexdigest(), issued.public_id))
        with pytest.raises(BusinessError) as error:
            services(original, clock)[0].redeem(raw, expected_course_id=None, request_id="modern-denial")
        assert error.value.code == "redemption_denied"


def make_legacy_client(original, clock, http_app_factory, tmp_path):
    from fastapi.testclient import TestClient
    from course_platform.admin.routes.auth import router as auth_router
    from course_platform.admin.routes.legacy import router
    from course_platform.admin.routes.entitlements import router as support_router
    from course_platform.security import CsrfService
    from course_platform.admin.security import RateLimiter
    from course_platform.settings import Settings
    from course_platform.operations.legacy import LegacyService
    rights, recovery, progress = services(original, clock)
    app = http_app_factory([auth_router, router, support_router], Settings(base_url="", api_key="",
        content_root=tmp_path, database_path=original.db, session_ttl_hours=72, environment="development", site_origin="http://testserver"),
        dict(admin_service=AdminService(original.db, clock=clock.now), csrf_service=CsrfService(original.db, clock=clock.now),
            rate_limiter=RateLimiter(original.db, clock=clock.now), legacy_service=LegacyService(original.db, clock=clock.now),
            entitlement_service=rights, recovery_service=recovery, progress_service=progress))
    return TestClient(app, client=("198.51.100.7", 50000), follow_redirects=False)


def test_legacy_detail_displays_historical_shanghai_summer_time(original, tmp_path, clock, http_app_factory):
    # Fixed UTC+8 display must fail: Shanghai was UTC+9 on these historical dates.
    from test_product_routes import login
    session_hash = hashlib.sha256(original.sessions[0].session_id.encode()).hexdigest()
    with transaction(original.db) as connection:
        connection.execute("UPDATE sessions SET created_at=?, expires_at=? WHERE session_hash=?",
            ("1991-07-01T00:00:00+00:00", "1991-07-02T00:00:00+00:00", session_hash))
        connection.execute("UPDATE progress SET updated_at=? WHERE session_hash=?",
            ("1991-07-01T00:00:00+00:00", session_hash))
    upgrade(original, tmp_path)
    owner_setup(original, clock)
    identity = rows(original.db, "SELECT entitlement_id FROM sessions WHERE session_hash=?", (session_hash,))[0]["entitlement_id"]
    with make_legacy_client(original, clock, http_app_factory, tmp_path) as client:
        login(client)
        response = client.get(f"/admin/entitlements/{identity}")
        assert response.status_code == 200
        assert "1991-07-01 09:00:00 Asia/Shanghai" in response.text
        assert "1991-07-02 09:00:00 Asia/Shanghai" in response.text
        assert "1991-07-01 08:00:00" not in response.text
    assert rows(original.db, "SELECT original_expires_at FROM legacy_entitlement_origins WHERE entitlement_id=?", (identity,)) == [
        dict(original_expires_at="1991-07-02T00:00:00+00:00")]


def test_owner_http_legacy_verify_then_separate_reset(original, tmp_path, clock, http_app_factory):
    import re
    from test_product_routes import login, post
    upgrade(original, tmp_path)
    owner_setup(original, clock)
    with make_legacy_client(original, clock, http_app_factory, tmp_path) as client:
        assert client.get("/admin/legacy-codes").status_code == 303
        login(client)
        page = client.get("/admin/legacy-codes")
        assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
        assert original.unused not in page.text
        assert 'href="/admin/entitlements/1"' in page.text
        detail = client.get("/admin/entitlements/1")
        assert 'action="/admin/entitlements/1/verify-legacy"' in detail.text
        assert "原截止" in detail.text and "新截止" in detail.text and "Asia/Shanghai" in detail.text
        values = dict(revision="1", purpose="test", verification_reason="Confirmed original holder", idempotency_key="http-verify",
            access_mode="days", access_days="7", online="on", update_policy="current_version",
            expires_at="2026-10-09T08:00:00+08:00", confirm="on")
        response = post(client, "/admin/entitlements/1/verify-legacy", values)
        assert response.status_code == 303 and "LK-" not in response.text
        assert rows(original.db, "SELECT * FROM recovery_credentials") == []
        reset = post(client, "/admin/entitlements/1/reset-credential", dict(revision="2", reason="Holder lost credential", idempotency_key="http-reset", confirm="on"))
        assert reset.status_code == 200
        key = re.search(r"LK-[A-Za-z0-9_-]{43}", reset.text).group()
        assert services(original, clock)[2].get_progress(services(original, clock)[1].restore(key, request_id="http-restore").session_id, original.course) == {1: True}


@pytest.mark.parametrize("fault,status", [("auth",401), ("csrf",403), ("origin",403), ("confirm",400), ("revision",409), ("body",413)])
@pytest.mark.parametrize("kind", ["code", "entitlement"])
def test_legacy_http_denials_are_private_audited_and_atomic(original, tmp_path, clock, http_app_factory, fault, status, kind):
    from test_product_routes import login
    upgrade(original, tmp_path)
    owner_setup(original, clock)
    with make_legacy_client(original, clock, http_app_factory, tmp_path) as client:
        login(client)
        before = rows(original.db, "SELECT * FROM entitlements")
        data = dict(csrf_token=client.cookies.get("coursmith_admin_csrf"), revision="1", purpose="test",
            verification_reason="Checked holder", idempotency_key="verify", access_mode="days", access_days="7",
            online="on", expires_at="2026-10-09T00:00:00+00:00", update_policy="current_version", confirm="on")
        headers = {"Origin": "http://testserver"}
        if fault == "auth":
            client.cookies.clear()
        elif fault == "csrf":
            data["csrf_token"] = "invalid"
        elif fault == "origin":
            headers["Origin"] = "http://evil.test"
        elif fault == "confirm":
            data.pop("confirm")
        elif fault == "revision":
            data["revision"] = "0"
        else:
            data["extra"] = "x"*65536
        path = "/admin/entitlements/1/verify-legacy" if kind == "entitlement" else "/admin/legacy-codes/1/resolve"
        response = client.post(path, data=data, headers=headers)
        assert response.status_code == status and response.headers["cache-control"] == "no-store"
        assert rows(original.db, "SELECT * FROM entitlements") == before
        assert rows(original.db, "SELECT * FROM legacy_verifications") == []
        assert len(rows(original.db, "SELECT * FROM admin_events WHERE action='legacy.verify' AND outcome='denied'")) == 1


def test_resolved_sale_cannot_be_relabelled_to_bypass_purchase_proof(original, tmp_path, clock):
    upgrade(original, tmp_path)
    actor, legacy = owner_setup(original, clock)
    product = activate(original, actor, clock)
    code_id = rows(original.db, "SELECT id FROM access_codes WHERE code_hash=?", (hashlib.sha256(original.unused.encode()).hexdigest(),))[0]["id"]
    legacy.resolve_code(actor, code_id, 1, product.data.policy, "sale", "Original sale holder")
    receipt = services(original, clock)[0].redeem(original.unused, expected_course_id=None, request_id="sale")
    with pytest.raises(BusinessError):
        legacy.verify_entitlement(actor, receipt.session.entitlement_id, 1, None, product.data.policy,
            clock.now()+timedelta(days=30), "test", "Relabel to avoid order", "relabel")


def test_original_sale_code_can_receive_real_order_proof_before_reset(original, tmp_path, clock):
    from course_platform.operations.orders import OrderInput, OrderService
    upgrade(original, tmp_path)
    actor, legacy = owner_setup(original, clock)
    product = activate(original, actor, clock)
    rights, recovery, progress = services(original, clock)
    code_id = rows(original.db, "SELECT id FROM access_codes WHERE code_hash=?", (hashlib.sha256(original.unused.encode()).hexdigest(),))[0]["id"]
    legacy.resolve_code(actor, code_id, 1, product.data.policy, "sale", "Original sale checked")
    receipt = rights.redeem(original.unused, expected_course_id=None, request_id="sale")
    progress.set_completed(receipt.session.session_id, original.course, 1, True)
    with pytest.raises(BusinessError):
        recovery.reset(actor, receipt.session.entitlement_id, 1, "Reason alone", "before-order")
    order = OrderService(original.db, clock=clock.now).record(actor, OrderInput(channel="shop", shop_id="shop",
        external_order_id="sale-original", product_id=product.id, paid_cents=100, paid_at=clock.now(), note="Checked external original order"), "order")
    legacy.verify_entitlement(actor, receipt.session.entitlement_id, 1, order.id, product.data.policy,
        receipt.entitlement_expires_at, "sale", "Bound original verified purchase", "sale-verify")
    key = recovery.reset(actor, receipt.session.entitlement_id, 2, "Original holder verified", "sale-reset").raw_key
    assert progress.get_progress(recovery.restore(key, request_id="sale-restored").session_id, original.course) == {1: True}
    with pytest.raises(BusinessError):
        recovery.restore(receipt.raw_recovery_key, request_id="old-lk")
    assert rows(original.db, "SELECT batch_id, order_id, created_by FROM access_codes WHERE id=?", (code_id,)) == [
        dict(batch_id=None, order_id=None, created_by=None)]


@pytest.mark.parametrize("bad_expiry", [False, True])
def test_explicit_no_fixed_expiry_does_not_infer_from_unknown_history(original, tmp_path, clock, bad_expiry):
    upgrade(original, tmp_path)
    actor, legacy = owner_setup(original, clock)
    policy = AccessPolicy(access_mode="no_fixed_expiry", access_days=None, online=True, pdf=False, zip=False, update_policy="current_version")
    expiry = clock.now()+timedelta(days=7) if bad_expiry else None
    if bad_expiry:
        with pytest.raises(BusinessError):
            legacy.verify_entitlement(actor, 1, 1, None, policy, expiry, "test", "Explicitly verified no fixed expiry", "no-fixed")
    else:
        legacy.verify_entitlement(actor, 1, 1, None, policy, None, "test", "Explicitly verified no fixed expiry", "no-fixed")
        key = services(original, clock)[1].reset(actor, 1, 2, "Holder verified", "reset").raw_key
        assert services(original, clock)[1].restore(key, request_id="no-fixed").session_expires_at == clock.now()+timedelta(hours=72)


def test_populated_v4_upgrade_preserves_existing_rights_and_storage(populated_v2, tmp_path):
    # Historical storage fixtures are SQL graphs, not ownership shortcuts.
    from test_migrations import operations_snapshot
    migrate_database(populated_v2, through_version=4, backup_path=tmp_path / "before-four.db")
    before = operations_snapshot(populated_v2)
    migrate_database(populated_v2, backup_path=tmp_path / "before-five.db")
    after = operations_snapshot(populated_v2)
    for table in ("products", "orders", "code_batches", "entitlements", "sessions", "progress", "entitlement_progress", "recovery_credentials", "admins", "admin_sessions", "events", "admin_events", "operation_requests"):
        if table in ("products", "admin_events"):
            assert after[table][:len(before[table])] == before[table]
        else:
            assert after[table] == before[table]
    assert rows(populated_v2, "SELECT created_by, status, access_days, update_policy FROM products WHERE course_id='c2'") == [
        dict(created_by=None, status="draft", access_days=None, update_policy=None)]
    assert operations_snapshot(tmp_path / "before-five.db") == before
    assert after["schema_migrations"][:4] == before["schema_migrations"]
    assert after["schema_migrations"][-1][0] == 5
    assert check_database(populated_v2)["foreign_keys"] == "ok"


def test_disabled_admin_does_not_revoke_already_verified_buyer_right(original, tmp_path, clock):
    upgrade(original, tmp_path)
    actor, legacy = owner_setup(original, clock)
    policy = AccessPolicy(access_mode="days", access_days=7, online=True, pdf=False, zip=False, update_policy="current_version")
    legacy.verify_entitlement(actor, 1, 1, None, policy, clock.now()+timedelta(days=7), "gift", "Confirmed original holder", "verify")
    _, recovery, progress = services(original, clock)
    key = recovery.reset(actor, 1, 2, "Holder verified", "reset").raw_key
    with transaction(original.db) as connection:
        connection.execute("UPDATE admins SET enabled=0")
    assert progress.get_progress(recovery.restore(key, request_id="buyer").session_id, original.course) == {1: True}
    with pytest.raises(BusinessError):
        recovery.reset(actor, 1, 3, "Disabled owner", "disabled-reset")


@pytest.mark.parametrize("used", [False, True])
def test_owner_http_resolves_only_chosen_legacy_code(original, tmp_path, clock, http_app_factory, used):
    from test_product_routes import login, post
    upgrade(original, tmp_path)
    actor, _ = owner_setup(original, clock)
    activate(original, actor, clock)
    raw = original.used if used else original.unused
    identity = rows(original.db, "SELECT id FROM access_codes WHERE code_hash=?", (hashlib.sha256(raw.encode()).hexdigest(),))[0]["id"]
    before = rows(original.db, "SELECT * FROM entitlements")
    with make_legacy_client(original, clock, http_app_factory, tmp_path) as client:
        login(client)
        data = dict(revision="1", purpose="gift", verification_reason="Verified original recipient", confirm="on")
        if not used:
            data.update(access_mode="days", access_days="30", online="on", update_policy="current_version")
        response = post(client, f"/admin/legacy-codes/{identity}/resolve", data)
        assert response.status_code == 303 and response.headers["cache-control"] == "no-store"
        assert "LK-" not in response.text and raw not in response.text
        assert rows(original.db, "SELECT * FROM entitlements") == before
        assert rows(original.db, "SELECT * FROM recovery_credentials") == []
        assert len(rows(original.db, "SELECT * FROM legacy_verifications WHERE code_id=?", (identity,))) == 1


@pytest.mark.parametrize("fault", ["other_policy", "refunded", "future_payment", "unknown_payment", "already_issued", "already_bound"])
def test_legacy_sale_order_proof_and_single_right_are_binding(original, tmp_path, clock, fault):
    from course_platform.operations.orders import OrderInput, OrderService
    upgrade(original, tmp_path)
    actor, legacy = owner_setup(original, clock)
    product = activate(original, actor, clock)
    orders = OrderService(original.db, clock=clock.now)
    order = orders.record(actor, OrderInput(channel="shop", shop_id="shop", external_order_id="proof",
        product_id=product.id, paid_cents=100, paid_at=clock.now(), note="Original external payment"), "record-proof")
    if fault == "already_issued":
        orders.issue(actor, order.id, 1, "issue-proof")
    elif fault == "already_bound":
        legacy.verify_entitlement(actor, 1, 1, order.id, product.data.policy, clock.now()+timedelta(days=7), "sale", "First verified holder", "first")
    else:
        with transaction(original.db) as connection:
            if fault == "refunded":
                connection.execute("UPDATE orders SET status='refunded' WHERE id=?", (order.id,))
            elif fault == "future_payment":
                connection.execute("UPDATE orders SET paid_at='2027-10-02T00:00:00+00:00' WHERE id=?", (order.id,))
            elif fault == "unknown_payment":
                connection.execute("UPDATE orders SET paid_at=NULL WHERE id=?", (order.id,))
    policy = product.data.policy.model_copy(update={"access_days": 7}) if fault == "other_policy" else product.data.policy
    target = 2 if fault == "already_bound" else 1
    with pytest.raises(BusinessError):
        legacy.verify_entitlement(actor, target, 1, order.id, policy, clock.now()+timedelta(days=7), "sale", "Must match actual original order", "reject")
    assert rows(original.db, "SELECT verified_at FROM entitlements WHERE id=?", (target,)) == [dict(verified_at=None)]


def test_legacy_verification_request_fault_rolls_back_proof_order_and_right(original, tmp_path, clock):
    from course_platform.operations.orders import OrderInput, OrderService
    upgrade(original, tmp_path)
    actor, legacy = owner_setup(original, clock)
    product = activate(original, actor, clock)
    order = OrderService(original.db, clock=clock.now).record(actor, OrderInput(channel="shop", shop_id="shop",
        external_order_id="atomic-proof", product_id=product.id, paid_cents=100, paid_at=clock.now(), note="Confirmed"), "record")
    with transaction(original.db) as connection:
        connection.execute("""CREATE TRIGGER fail_verify_request BEFORE INSERT ON operation_requests
            WHEN NEW.action='legacy.verify' BEGIN SELECT RAISE(ABORT, 'private fault'); END""")
    before = rows(original.db, "SELECT * FROM entitlements")
    with pytest.raises(BusinessError):
        legacy.verify_entitlement(actor, 1, 1, order.id, product.data.policy, clock.now()+timedelta(days=7), "sale", "Confirmed holder", "fault")
    assert rows(original.db, "SELECT * FROM entitlements") == before
    assert rows(original.db, "SELECT * FROM legacy_verifications") == []
    assert rows(original.db, "SELECT revision, delivery_state FROM orders") == [dict(revision=1, delivery_state="recorded")]
    assert rows(original.db, "SELECT * FROM admin_events WHERE action='legacy.verify' AND outcome='success'") == []
