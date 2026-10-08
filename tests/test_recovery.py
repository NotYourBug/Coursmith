"""Real recovery consumers catch lost progress, extra devices and unsafe resets."""

import hashlib
import zipfile
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from course_platform.database import transaction, to_db_time
from course_platform.delivery.recovery import RecoveryService
from course_platform.domain import BusinessError
from course_platform.operations.codes import BatchInput
from test_entitlements import rows


def test_fourth_device_requires_confirmation_and_preserves_progress(recovery_service, redeemed,
        progress_service, entitlement_service, clock, db_path):
    first = redeemed.session
    progress_service.set_completed(first.session_id, first.course_id, 1, True)
    clock.advance(seconds=1)
    second = recovery_service.restore(redeemed.raw_recovery_key, request_id="second")
    clock.advance(seconds=1)
    third = recovery_service.restore(redeemed.raw_recovery_key, request_id="third")
    with pytest.raises(BusinessError) as error:
        recovery_service.restore(redeemed.raw_recovery_key, request_id="fourth")
    assert (error.value.code, error.value.status_code) == ("device_confirmation_required", 409)
    assert len(rows(db_path, "SELECT session_hash FROM sessions")) == 3
    fourth = recovery_service.restore(redeemed.raw_recovery_key, evict_oldest=True, request_id="confirmed")
    with pytest.raises(BusinessError):
        entitlement_service.require_session(first.session_id, first.course_id)
    for grant in (second, third, fourth):
        assert progress_service.get_progress(grant.session_id, grant.course_id) == {1: True}
        assert entitlement_service.require_session(grant.session_id, grant.course_id).entitlement_expires_at == redeemed.entitlement_expires_at


def test_reset_revokes_old_credentials_and_sessions_without_extending_access(recovery_service,
        verified_gift, progress_service, entitlement_service, actor, clock, db_path):
    old = verified_gift
    progress_service.set_completed(old.session.session_id, old.session.course_id, 1, True)
    second = recovery_service.restore(old.raw_recovery_key, request_id="second")
    clock.advance(hours=1)
    receipt = recovery_service.reset(actor, old.session.entitlement_id, 1, "已核对记录，凭证遗失", "reset")
    assert receipt.raw_key.startswith("LK-") and not receipt.replayed
    assert receipt.raw_key not in repr(receipt)
    with pytest.raises(BusinessError):
        recovery_service.restore(old.raw_recovery_key, request_id="old")
    for grant in (old.session, second):
        with pytest.raises(BusinessError):
            entitlement_service.require_session(grant.session_id, grant.course_id)
    new = recovery_service.restore(receipt.raw_key, request_id="new")
    assert progress_service.get_progress(new.session_id, new.course_id) == {1: True}
    assert entitlement_service.require_session(new.session_id, new.course_id).entitlement_expires_at == old.entitlement_expires_at
    assert receipt.raw_key.encode() not in db_path.read_bytes()


def test_concurrent_restores_never_exceed_three_sessions(recovery_service, redeemed, db_path):
    barrier = Barrier(6)
    def restore(index):
        barrier.wait(timeout=5)
        try:
            return recovery_service.restore(redeemed.raw_recovery_key, request_id=f"race-{index}")
        except BusinessError as error:
            return error
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(restore, range(6)))
    assert sum(not isinstance(result, BusinessError) for result in results) == 2
    assert {result.code for result in results if isinstance(result, BusinessError)} == {"device_confirmation_required"}
    assert len(rows(db_path, "SELECT session_hash FROM sessions WHERE revoked_at IS NULL")) == 3


def test_reset_response_loss_does_not_reveal_old_key(recovery_service, verified_gift, actor, db_path):
    first = recovery_service.reset(actor, verified_gift.session.entitlement_id, 1, "凭证丢失", "same")
    replay = recovery_service.reset(actor, verified_gift.session.entitlement_id, 1, "凭证丢失", "same")
    assert replay.replayed and replay.raw_key is None and replay.entitlement_id == first.entitlement_id
    assert len(rows(db_path, "SELECT id FROM recovery_credentials")) == 2
    with pytest.raises(BusinessError) as error:
        recovery_service.reset(actor, first.entitlement_id, 2, "另一请求", "same")
    assert error.value.code == "idempotency_conflict"
    fresh = recovery_service.reset(actor, first.entitlement_id, 2, "明确再次重置", "fresh")
    assert fresh.raw_key != first.raw_key


def test_restore_after_revoke_is_generic_and_contains_no_order_id(recovery_service, redeemed,
        entitlement_service, actor):
    entitlement_service.revoke(actor, redeemed.session.entitlement_id, 1, "已核验退款", "revoke")
    errors = []
    for key in (redeemed.raw_recovery_key, "LK-" + "x" * 43):
        with pytest.raises(BusinessError) as error:
            recovery_service.restore(key, request_id="denied")
        errors.append((error.value.code, error.value.message, error.value.status_code))
    assert errors[0] == errors[1] and errors[0][0] == "recovery_denied"


def test_owner_reset_requires_recorded_purchase_verification(recovery_service, redeemed, actor,
        db_path, clock):
    for verified in (False, True):
        if verified:
            with transaction(db_path) as connection:
                connection.execute("UPDATE entitlements SET verified_at=?, verified_by=1, verified_reason='same course'",
                                   (to_db_time(clock.now()),))
        with pytest.raises(BusinessError) as error:
            recovery_service.reset(actor, redeemed.session.entitlement_id, 1, "课程相同，声称已购买", f"sale-{verified}")
        assert error.value.code == "purchase_verification_required"
    assert len(rows(db_path, "SELECT id FROM recovery_credentials")) == 1


def test_equal_clock_eviction_uses_session_hash(recovery_service, redeemed, db_path, entitlement_service):
    recovery_service.restore(redeemed.raw_recovery_key, request_id="two")
    recovery_service.restore(redeemed.raw_recovery_key, request_id="three")
    tokens = sorted(["fixed-a", "fixed-b", "fixed-c"], key=lambda token: hashlib.sha256(token.encode()).hexdigest(), reverse=True)
    with transaction(db_path) as connection:
        for session, token in zip(connection.execute("SELECT rowid FROM sessions ORDER BY rowid").fetchall(), tokens):
            connection.execute("UPDATE sessions SET session_hash=? WHERE rowid=?", (hashlib.sha256(token.encode()).hexdigest(), session[0]))
    recovery_service.restore(redeemed.raw_recovery_key, evict_oldest=True, request_id="four")
    with pytest.raises(BusinessError):
        entitlement_service.require_session(tokens[-1], "fixture-course")
    for token in tokens[:-1]:
        entitlement_service.require_session(token, "fixture-course")


@pytest.mark.parametrize("fault", ["expired", "revoked", "snapshot", "content", "null_legacy", "key", "confirmation"])
def test_restore_denies_invalid_rights_generically(recovery_service, redeemed, db_path, clock, fixture_package, fault):
    key = redeemed.raw_recovery_key
    with transaction(db_path) as connection:
        if fault == "expired":
            connection.execute("UPDATE entitlements SET expires_at=?", (to_db_time(clock.now()),))
        elif fault == "revoked":
            connection.execute("UPDATE recovery_credentials SET revoked_at=?", (to_db_time(clock.now()),))
        elif fault in ("snapshot", "null_legacy"):
            connection.execute("UPDATE entitlements SET issued_policy_json=NULL")
            if fault == "null_legacy":
                connection.execute("UPDATE access_codes SET issued_policy_json=NULL, created_by=NULL")
                connection.execute("UPDATE entitlements SET created_by=NULL, legacy_state='pending_verification'")
    if fault == "content":
        (fixture_package / "chapters/01.html").write_bytes(b"broken content")
    if fault == "key":
        key = "LK-invalid"
    with pytest.raises(BusinessError) as error:
        recovery_service.restore(key, evict_oldest="on" if fault == "confirmation" else False, request_id="bad")
    assert (error.value.code, error.value.status_code) == ("recovery_denied", 403)
    assert len(rows(db_path, "SELECT session_hash FROM sessions")) == 1
    assert key not in error.value.message


@pytest.mark.parametrize("purpose", ["test", "gift"])
def test_reset_checks_actual_issued_purpose(recovery_service, verified_gift, actor, db_path, purpose):
    with transaction(db_path) as connection:
        connection.execute("UPDATE entitlements SET purpose=?", (purpose,))
        connection.execute("UPDATE access_codes SET purpose='sale'")
    with pytest.raises(BusinessError) as error:
        recovery_service.reset(actor, 1, 1, "人工确认赠送", "mismatch")
    assert error.value.code == "verification_required"
    assert len(rows(db_path, "SELECT id FROM recovery_credentials")) == 1


@pytest.mark.parametrize("fault", ["credential_insert", "success_audit"])
def test_reset_rolls_back_all_changes_on_database_failure(recovery_service, verified_gift, actor, db_path, fault):
    tables = ("entitlements", "recovery_credentials", "sessions", "operation_requests")
    before = {table: rows(db_path, f"SELECT * FROM {table}") for table in tables}
    with transaction(db_path) as connection:
        if fault == "credential_insert":
            connection.execute("CREATE TRIGGER fail_reset BEFORE INSERT ON recovery_credentials BEGIN SELECT RAISE(ABORT, 'fail'); END")
        else:
            connection.execute("""CREATE TRIGGER fail_reset BEFORE INSERT ON admin_events
                WHEN NEW.action='recovery.reset' AND NEW.outcome='success' BEGIN SELECT RAISE(ABORT, 'fail'); END""")
    with pytest.raises(BusinessError):
        recovery_service.reset(actor, 1, 1, "遗失", "rollback")
    assert {table: rows(db_path, f"SELECT * FROM {table}") for table in tables} == before
    assert len(rows(db_path, "SELECT id FROM admin_events WHERE action='recovery.reset' AND outcome='denied'")) == 1


def test_denial_duplicates_are_suppressed_durably(recovery_service, redeemed, actor, db_path):
    for _ in range(2):
        error = BusinessError("invalid_origin", "Denied.", 403)
        recovery_service.record_denial(actor, "recovery.reset", 1, error, request_id="same-boundary")
        assert error.denial_recorded
    assert len(rows(db_path, "SELECT id FROM admin_events WHERE action='recovery.reset' AND outcome='denied'")) == 1


def test_restore_uses_shared_configured_ttl_and_ignores_inactive_devices(db_path, clock, redeemed,
        entitlement_service):
    from datetime import timedelta
    from course_platform.delivery.entitlements import EntitlementService
    shared = EntitlementService(db_path, clock=clock.now, session_ttl_hours=6)
    recovery = RecoveryService(db_path, clock=clock.now, session_ttl_hours=6, entitlement_service=shared)
    expired = recovery.restore(redeemed.raw_recovery_key, request_id="expired")
    revoked = recovery.restore(redeemed.raw_recovery_key, request_id="revoked")
    with transaction(db_path) as connection:
        connection.execute("UPDATE sessions SET expires_at=? WHERE session_hash=?",
            (to_db_time(clock.now()), hashlib.sha256(expired.session_id.encode()).hexdigest()))
        connection.execute("UPDATE sessions SET revoked_at=? WHERE session_hash=?",
            (to_db_time(clock.now()), hashlib.sha256(revoked.session_id.encode()).hexdigest()))
    grant = recovery.restore(redeemed.raw_recovery_key, request_id="active")
    assert grant.session_expires_at == clock.now() + timedelta(hours=6)
    clock.advance(days=29, hours=23)
    grant = recovery.restore(redeemed.raw_recovery_key, request_id="capped")
    assert grant.session_expires_at == redeemed.entitlement_expires_at
    with pytest.raises(ValueError):
        RecoveryService(db_path, clock=clock.now, session_ttl_hours=7, entitlement_service=shared)


@pytest.mark.parametrize("format_flag", ["pdf", "zip"])
def test_restore_download_only_rights_keeps_online_denied(recovery_service, active_product,
        product_service, code_service, entitlement_service, progress_service, fixture_package, db_path, actor, format_flag):
    from course_platform.content_inspection import inspect_package
    from course_platform.operations.products import SalesChecklist
    downloads = fixture_package / "downloads"
    downloads.mkdir(exist_ok=True)
    if format_flag == "pdf":
        (downloads / "course.pdf").write_bytes(b"%PDF-1.7\nfixture")
    else:
        with zipfile.ZipFile(downloads / "course.zip", "w") as archive:
            for name in ("manifest.json", "index.html", "SOURCES.txt", "LICENSE.txt", "chapters/01.html"):
                archive.write(fixture_package / name, name)
    with transaction(db_path) as connection:
        connection.execute("UPDATE courses SET package_hash=?", (inspect_package(fixture_package).fingerprint,))
    changed = product_service.update(actor, active_product.id, active_product.revision,
        active_product.data.model_copy(update={"policy": active_product.data.policy.model_copy(update={"online": False, format_flag: True})}))
    product_service.activate(actor, changed.id, changed.revision, SalesChecklist(quality=True, sources=True, ai=True, mobile=True, downloads=True))
    issued = code_service.issue_batch(actor, BatchInput(product_id=active_product.id, purpose="gift"), "download-gift").codes[0]
    redeemed = entitlement_service.redeem(issued.raw_code, expected_course_id=None, request_id="download")
    reset = recovery_service.reset(actor, redeemed.session.entitlement_id, 1, "核对受赠用途", "download-reset")
    grant = recovery_service.restore(reset.raw_key, request_id="download-restore")
    authorized = entitlement_service.require_session(grant.session_id, grant.course_id)
    assert authorized.issued_policy.access.online is False
    assert getattr(authorized.issued_policy.access, format_flag) is True
    with pytest.raises(BusinessError) as error:
        progress_service.get_progress(grant.session_id, grant.course_id)
    assert error.value.code == "online_unavailable"


@pytest.mark.parametrize("fault", ["unverified", "unpaid", "refunded", "snapshot", "purpose"])
def test_sale_reset_rejects_incomplete_bound_order_evidence(recovery_service, active_product,
        code_service, entitlement_service, actor, db_path, clock, fault):
    from course_platform.operations.products import IssuedPolicy
    seed = code_service.issue_batch(actor, BatchInput(product_id=active_product.id, purpose="sale"), "snapshot-seed")
    with transaction(db_path, immediate=True) as connection:
        snapshot = connection.execute("SELECT issued_policy_json FROM access_codes WHERE id=1").fetchone()[0]
        order_id = connection.execute("""INSERT INTO orders
            (product_id, channel, shop, external_order_id, amount_cents, created_by, paid_at,
             delivery_state, issued_policy_json) VALUES (?, 'manual', 'shop', 'verified-order', 100, 1, ?, 'recorded', ?)""",
            (active_product.id, to_db_time(clock.now()), snapshot)).lastrowid
        bound = code_service.issue_in_tx(connection, actor, IssuedPolicy.model_validate_json(snapshot),
            BatchInput(product_id=active_product.id, purpose="sale"), order_id=order_id, idempotency_key="bound-code")
    redeemed = entitlement_service.redeem(bound.codes[0].raw_code, expected_course_id=None, request_id="bound-sale")
    with transaction(db_path) as connection:
        if fault != "unverified":
            connection.execute("UPDATE entitlements SET verified_at=?, verified_by=1, verified_reason='核验店铺对应订单'",
                               (to_db_time(clock.now()),))
        if fault == "unpaid":
            connection.execute("UPDATE orders SET paid_at=NULL")
        elif fault == "refunded":
            connection.execute("UPDATE orders SET status='refunded'")
        elif fault == "snapshot":
            connection.execute("UPDATE orders SET issued_policy_json=NULL")
        elif fault == "purpose":
            connection.execute("UPDATE code_batches SET purpose='gift' WHERE id=?", (bound.batch_id,))
    with pytest.raises(BusinessError) as error:
        recovery_service.reset(actor, redeemed.session.entitlement_id, 1, "已有同课程订单", "bound-reset")
    assert error.value.code == ("verification_required" if fault == "purpose" else "purchase_verification_required")
    assert len(rows(db_path, "SELECT id FROM recovery_credentials")) == 1
    assert seed.codes[0].raw_code.encode() not in db_path.read_bytes()


def test_restore_rolls_back_eviction_and_session_when_success_audit_fails(recovery_service, redeemed, db_path):
    recovery_service.restore(redeemed.raw_recovery_key, request_id="two")
    recovery_service.restore(redeemed.raw_recovery_key, request_id="three")
    before = rows(db_path, "SELECT * FROM sessions")
    with transaction(db_path) as connection:
        connection.execute("""CREATE TRIGGER fail_restore BEFORE INSERT ON admin_events
            WHEN NEW.action='recovery.recover' AND NEW.outcome='success' BEGIN SELECT RAISE(ABORT, 'fail'); END""")
    with pytest.raises(BusinessError) as error:
        recovery_service.restore(redeemed.raw_recovery_key, evict_oldest=True, request_id="four")
    assert error.value.code == "recovery_denied"
    assert rows(db_path, "SELECT * FROM sessions") == before
    assert rows(db_path, "SELECT id FROM admin_events WHERE action='session.revoke'") == []


def test_concurrent_same_reset_has_one_plaintext_receipt(recovery_service, verified_gift, actor, db_path):
    barrier = Barrier(2)
    def reset(_):
        barrier.wait(timeout=5)
        return recovery_service.reset(actor, 1, 1, "已核对受赠者", "same-race")
    with ThreadPoolExecutor(2) as pool:
        receipts = list(pool.map(reset, range(2)))
    assert sorted(receipt.replayed for receipt in receipts) == [False, True]
    assert sum(receipt.raw_key is not None for receipt in receipts) == 1
    assert len(rows(db_path, "SELECT id FROM recovery_credentials")) == 2
    assert rows(db_path, "SELECT revision FROM entitlements") == [{"revision": 2}]
