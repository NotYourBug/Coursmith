"""Real issuer consumers: atomic redemption, immutable promises and authorization.

These tests catch duplicate grants, partial writes, current-policy substitution,
credential disclosure, stale batch writes and authorization after revocation.
"""

import hashlib
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from threading import Barrier

import pytest
from pydantic import ValidationError

from course_platform.database import connect, open_readonly, to_db_time, transaction
from course_platform.delivery.entitlements import EntitlementService
from course_platform.domain import Actor, BusinessError
from course_platform.operations.codes import BatchInput, CodeService
from course_platform.operations.products import AccessPolicy, SalesChecklist


def rows(path, sql, args=()):
    with closing(open_readonly(path)) as connection:
        return [dict(row) for row in connection.execute(sql, args)]


def test_concurrent_redeem_creates_exactly_one_entitlement(db_path, clock, issued_code):
    barrier = Barrier(2)

    def redeem(_):
        service = EntitlementService(db_path, clock=clock.now)
        barrier.wait(timeout=5)
        try:
            return service.redeem(issued_code.raw_code, expected_course_id=None, request_id="race")
        except BusinessError as error:
            return error

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(redeem, range(2)))
    grants = [result for result in results if not isinstance(result, BusinessError)]
    errors = [result for result in results if isinstance(result, BusinessError)]
    assert len(grants) == len(errors) == 1
    assert errors[0].code == "redemption_denied"
    for table in ("entitlements", "recovery_credentials", "sessions"):
        assert len(rows(db_path, f"SELECT * FROM {table}")) == 1
    assert rows(db_path, "SELECT revoked_at FROM recovery_credentials") == [{"revoked_at": None}]
    assert rows(db_path, "SELECT revoked_at FROM sessions") == [{"revoked_at": None}]
    assert rows(db_path, "SELECT used_at FROM access_codes")[0]["used_at"] == to_db_time(clock.now())
    assert rows(db_path, "SELECT created_at FROM entitlements")[0]["created_at"] == to_db_time(clock.now())
    assert rows(db_path, "SELECT revision FROM code_batches") == [{"revision": 2}]
    assert sorted(row["outcome"] for row in rows(db_path, "SELECT outcome FROM admin_events WHERE action='code.redeem'")) == ["denied", "success"]


def test_access_expiry_uses_issued_policy_not_current_product(entitlement_service, issued_code,
        active_product, product_service, actor, db_path, clock):
    snapshot = rows(db_path, "SELECT issued_policy_json FROM access_codes")[0]["issued_policy_json"]
    changed = product_service.update(actor, active_product.id, active_product.revision,
        active_product.data.model_copy(update={"title": "new", "policy": active_product.data.policy.model_copy(update={"access_days": 1})}))
    activated = product_service.activate(actor, changed.id, changed.revision,
        SalesChecklist(quality=True, sources=True, ai=True, mobile=True, downloads=True))
    product_service.set_status(actor, activated.id, activated.revision, "paused")
    clock.advance(days=2)
    receipt = entitlement_service.redeem(issued_code.raw_code, expected_course_id=None, request_id="original-policy")
    assert receipt.entitlement_expires_at == clock.now() + timedelta(days=30)
    assert receipt.session.session_expires_at == clock.now() + timedelta(hours=72)
    assert rows(db_path, "SELECT issued_policy_json FROM entitlements") == [{"issued_policy_json": snapshot}]
    authorized = entitlement_service.require_session(receipt.session.session_id, "fixture-course")
    assert authorized.issued_policy.title == "可售课程"
    assert authorized.issued_policy.access.access_days == 30
    clock.advance(hours=1)
    with transaction(db_path, immediate=True) as connection:
        recovered = entitlement_service.create_session_in_tx(connection, receipt.session.entitlement_id, evict_oldest=False)
    assert entitlement_service.require_session(recovered.session_id, recovered.course_id).entitlement_expires_at == receipt.entitlement_expires_at


@pytest.mark.parametrize("fault", ["expired", "voided", "course", "unknown", "used", "content", "snapshot", "version", "fingerprint"])
def test_expired_revoked_and_cross_course_codes_are_denied_and_audited(entitlement_service,
        issued_code, db_path, clock, fixture_package, fault):
    raw, expected = issued_code.raw_code, None
    with transaction(db_path) as connection:
        if fault == "expired":
            connection.execute("UPDATE access_codes SET expires_at=?", (to_db_time(clock.now()),))
        elif fault == "voided":
            connection.execute("UPDATE access_codes SET voided_at=?", (to_db_time(clock.now()),))
        elif fault == "snapshot":
            connection.execute("UPDATE access_codes SET issued_policy_json=NULL")
        elif fault == "version":
            connection.execute("UPDATE courses SET version='2'")
        elif fault == "fingerprint":
            connection.execute("UPDATE access_codes SET package_hash=?", ("0" * 64,))
    if fault == "course":
        expected = "another-course"
    if fault == "unknown":
        raw = "CS-" + "x" * 32
    if fault == "used":
        entitlement_service.redeem(raw, expected_course_id=None, request_id="first")
    if fault == "content":
        (fixture_package / "chapters/01.html").write_text("changed", encoding="utf8")
    before = rows(db_path, "SELECT * FROM entitlements")
    with pytest.raises(BusinessError) as caught:
        entitlement_service.redeem(raw, expected_course_id=expected, request_id="denial")
    assert caught.value.code == "redemption_denied"
    assert raw not in str(caught.value) and raw not in repr(caught.value)
    assert rows(db_path, "SELECT * FROM entitlements") == before
    denied = rows(db_path, "SELECT * FROM admin_events WHERE action='code.redeem' AND outcome='denied'")
    assert len(denied) == 1
    assert raw not in json.dumps(denied)


@pytest.mark.parametrize("table,event", [("access_codes", "update"), ("entitlements", "insert"), ("recovery_credentials", "insert"),
    ("sessions", "insert"), ("code_batches", "update"), ("orders", "update"), ("admin_events", "audit")])
def test_redemption_fault_rolls_back_all_state(entitlement_service, code_service, product_service,
        active_product, actor, db_path, table, event):
    with transaction(db_path, immediate=True) as connection:
        policy = product_service.require_sale_ready_in_tx(connection, active_product.id)
        connection.execute("""INSERT INTO orders (id, product_id, channel, shop, external_order_id,
            amount_cents, created_by, issued_policy_json, paid_at, delivery_state, delivered_at)
            VALUES (1, 1, 'store', 'shop', 'private-order', 123, 1, ?, '2026-10-01', 'delivered', '2026-10-02')""", (policy.model_dump_json(),))
        code = code_service.issue_in_tx(connection, actor, policy, BatchInput(product_id=1, purpose="sale"),
            order_id=1, idempotency_key="bound").codes[0]
        when = "WHEN NEW.action='code.redeem' AND NEW.outcome='success'" if event == "audit" else ""
        operation = "INSERT" if event in ("insert", "audit") else "UPDATE"
        connection.execute(f"CREATE TRIGGER fail_redemption BEFORE {operation} ON {table} {when} BEGIN SELECT RAISE(ABORT, 'private-order'); END")
    before = {name: rows(db_path, f"SELECT * FROM {name}") for name in
              ("access_codes", "code_batches", "entitlements", "recovery_credentials", "sessions", "orders", "operation_requests")}
    with pytest.raises(BusinessError) as caught:
        entitlement_service.redeem(code.raw_code, expected_course_id=None, request_id="fault")
    assert "private-order" not in str(caught.value)
    assert code.raw_code not in str(caught.value)
    for name, state in before.items():
        assert rows(db_path, f"SELECT * FROM {name}") == state
    assert len(rows(db_path, "SELECT id FROM admin_events WHERE action='code.redeem' AND outcome='denied'")) == 1


@pytest.mark.parametrize("refunded", [False, True])
def test_bound_order_keeps_original_promises_and_business_status(entitlement_service, code_service,
        product_service, active_product, actor, db_path, refunded):
    with transaction(db_path, immediate=True) as connection:
        policy = product_service.require_sale_ready_in_tx(connection, active_product.id)
        connection.execute("""INSERT INTO orders (id, product_id, channel, shop, external_order_id,
            amount_cents, created_by, issued_policy_json, paid_at, delivery_state, delivered_at)
            VALUES (1, 1, 'store', 'shop', 'private-order', 123, 1, ?, '2026-10-01', 'delivered', '2026-10-02')""", (policy.model_dump_json(),))
        code = code_service.issue_in_tx(connection, actor, policy, BatchInput(product_id=1, purpose="sale"),
            order_id=1, idempotency_key="bound").codes[0]
        if refunded:
            connection.execute("UPDATE orders SET status='refunded', refunded_at='2026-10-02', refund_reason='refund'")
    before = rows(db_path, "SELECT * FROM orders")[0]
    if refunded:
        with pytest.raises(BusinessError):
            entitlement_service.redeem(code.raw_code, expected_course_id=None, request_id="refund-denied")
        assert rows(db_path, "SELECT used_at FROM access_codes") == [{"used_at": None}]
        assert rows(db_path, "SELECT * FROM orders")[0] == before
    else:
        receipt = entitlement_service.redeem(code.raw_code, expected_course_id=None, request_id="bound")
        after = rows(db_path, "SELECT * FROM orders")[0]
        assert after == before | {"delivery_state": "activated", "revision": before["revision"] + 1}
        assert rows(db_path, "SELECT order_id, expires_at FROM entitlements") == [{"order_id": 1, "expires_at": to_db_time(receipt.entitlement_expires_at)}]


def test_policy_without_fixed_expiry_requires_explicit_choice(entitlement_service, code_service,
        product_service, active_product, actor, clock):
    with pytest.raises(ValidationError):
        AccessPolicy(access_days=None, online=True, pdf=False, zip=False, update_policy="current_version")
    with pytest.raises(ValidationError):
        AccessPolicy(access_mode="days", access_days=None, online=True, pdf=False, zip=False, update_policy="current_version")
    policy = AccessPolicy(access_mode="no_fixed_expiry", access_days=None, online=True, pdf=False, zip=False, update_policy="current_version")
    changed = product_service.update(actor, active_product.id, active_product.revision, active_product.data.model_copy(update={"policy": policy}))
    product_service.activate(actor, changed.id, changed.revision, SalesChecklist(quality=True, sources=True, ai=True, mobile=True, downloads=True))
    code = code_service.issue_batch(actor, BatchInput(product_id=1, purpose="sale"), "unlimited").codes[0]
    receipt = entitlement_service.redeem(code.raw_code, expected_course_id=None, request_id="unlimited")
    assert receipt.entitlement_expires_at is None
    assert receipt.session.session_expires_at == clock.now() + timedelta(hours=72)


def test_sensitive_credentials_are_hashed_and_omitted_from_repr(redeemed, issued_code, db_path, caplog):
    secrets = (issued_code.raw_code, redeemed.raw_recovery_key, redeemed.session.session_id, redeemed.session.csrf_token)
    assert redeemed.raw_recovery_key.startswith("LK-") and len(redeemed.raw_recovery_key) == 46
    assert len(redeemed.session.session_id) == len(redeemed.session.csrf_token) == 43
    dump = db_path.read_bytes().decode("latin1") + repr(redeemed) + repr(redeemed.session) + caplog.text
    assert all(secret not in dump for secret in secrets)
    session = rows(db_path, "SELECT session_hash, csrf_hash FROM sessions")[0]
    assert session == {"session_hash": hashlib.sha256(secrets[2].encode()).hexdigest(), "csrf_hash": hashlib.sha256(secrets[3].encode()).hexdigest()}
    assert rows(db_path, "SELECT credential_hash FROM recovery_credentials")[0]["credential_hash"] == hashlib.sha256(secrets[1].encode()).hexdigest()


@pytest.mark.parametrize("fault", ["expired_session", "expired_entitlement", "revoked_session", "revoked_entitlement", "course", "content", "version", "null_snapshot", "csrf"])
def test_session_authorization_checks_every_boundary(entitlement_service, redeemed, db_path, clock, fixture_package, fault):
    with transaction(db_path) as connection:
        if fault == "expired_session":
            connection.execute("UPDATE sessions SET expires_at=?", (to_db_time(clock.now()),))
        elif fault == "expired_entitlement":
            connection.execute("UPDATE entitlements SET expires_at=?", (to_db_time(clock.now()),))
        elif fault == "revoked_session":
            connection.execute("UPDATE sessions SET revoked_at=?", (to_db_time(clock.now()),))
        elif fault == "revoked_entitlement":
            connection.execute("UPDATE entitlements SET revoked_at=?", (to_db_time(clock.now()),))
        elif fault == "version":
            connection.execute("UPDATE courses SET version='2'")
        elif fault == "null_snapshot":
            connection.execute("UPDATE entitlements SET issued_policy_json=NULL")
        elif fault == "csrf":
            connection.execute("UPDATE sessions SET csrf_hash=NULL")
    if fault == "content":
        path = fixture_package / "chapters/01.html"
        path.write_bytes(path.read_bytes().replace(b"html", b"HTML", 1))
    with pytest.raises(BusinessError) as caught:
        entitlement_service.require_session(redeemed.session.session_id, "other" if fault == "course" else redeemed.session.course_id)
    assert caught.value.code == "session_denied"


def test_configured_session_ttl_is_capped_by_entitlement(db_path, clock, issued_code):
    service = EntitlementService(db_path, clock=clock.now, session_ttl_hours=1000)
    receipt = service.redeem(issued_code.raw_code, expected_course_id=None, request_id="capped")
    assert receipt.session.session_expires_at == clock.now() + timedelta(days=30)
    service = EntitlementService(db_path, clock=clock.now, session_ttl_hours=6)
    with transaction(db_path, immediate=True) as connection:
        grant = service.create_session_in_tx(connection, receipt.session.entitlement_id, evict_oldest=False)
    assert grant.session_expires_at == clock.now() + timedelta(hours=6)


@pytest.mark.parametrize("format_flag", ["pdf", "zip"])
def test_download_only_policy_authorizes_generic_sessions_but_denies_progress(entitlement_service,
        progress_service, code_service, product_service, active_product, actor, db_path, fixture_package,
        format_flag):
    from course_platform.content_inspection import inspect_package

    downloads = fixture_package / "downloads"
    downloads.mkdir(exist_ok=True)
    if format_flag == "pdf":
        (downloads / "course.pdf").write_bytes(b"%PDF-1.7\nfixture")
    else:
        with zipfile.ZipFile(downloads / "course.zip", "w") as archive:
            for name in ("manifest.json", "index.html", "SOURCES.txt", "LICENSE.txt", "chapters/01.html"):
                archive.write(fixture_package / name, name)
    inspection = inspect_package(fixture_package)
    with transaction(db_path) as connection:
        connection.execute("UPDATE courses SET package_hash=?", (inspection.fingerprint,))
    policy = active_product.data.policy.model_copy(update={"online": False, format_flag: True})
    changed = product_service.update(actor, active_product.id, active_product.revision,
        active_product.data.model_copy(update={"policy": policy}))
    product_service.activate(actor, changed.id, changed.revision, SalesChecklist(quality=True, sources=True, ai=True, mobile=True, downloads=True))
    code = code_service.issue_batch(actor, BatchInput(product_id=1, purpose="sale"), "download-only").codes[0]
    receipt = entitlement_service.redeem(code.raw_code, expected_course_id=None, request_id="download-only")
    first = receipt.session
    authorized = entitlement_service.require_session(first.session_id, first.course_id)
    assert authorized.issued_policy.access.online is False
    assert getattr(authorized.issued_policy.access, format_flag) is True
    with transaction(db_path, immediate=True) as connection:
        second = entitlement_service.create_session_in_tx(connection, first.entitlement_id, evict_oldest=False)
    assert entitlement_service.require_session(second.session_id, second.course_id).entitlement_id == first.entitlement_id
    for session in (first, second):
        with pytest.raises(BusinessError):
            progress_service.set_completed(session.session_id, session.course_id, 1, True)
        with pytest.raises(BusinessError):
            progress_service.get_progress(session.session_id, session.course_id)
    assert rows(db_path, "SELECT * FROM entitlement_progress") == []


def test_session_device_limit_requires_explicit_eviction(entitlement_service, redeemed, db_path, clock):
    grants = [redeemed.session]
    for _ in range(2):
        clock.advance(seconds=1)
        with transaction(db_path, immediate=True) as connection:
            grants.append(entitlement_service.create_session_in_tx(connection, redeemed.session.entitlement_id, evict_oldest=False))
    with pytest.raises(BusinessError) as caught:
        with transaction(db_path, immediate=True) as connection:
            entitlement_service.create_session_in_tx(connection, redeemed.session.entitlement_id, evict_oldest=False)
    assert caught.value.code == "session_limit"
    assert len(rows(db_path, "SELECT * FROM sessions WHERE revoked_at IS NULL")) == 3
    with transaction(db_path, immediate=True) as connection:
        fourth = entitlement_service.create_session_in_tx(connection, redeemed.session.entitlement_id, evict_oldest=True)
    with pytest.raises(BusinessError):
        entitlement_service.require_session(grants[0].session_id, grants[0].course_id)
    for grant in [*grants[1:], fourth]:
        assert entitlement_service.require_session(grant.session_id, grant.course_id).entitlement_expires_at == redeemed.entitlement_expires_at


def test_equal_timestamp_eviction_uses_hash_order_not_insertion_order(entitlement_service,
        progress_service, redeemed, db_path, clock):
    grants = [redeemed.session]
    progress_service.set_completed(grants[0].session_id, grants[0].course_id, 1, True)
    for _ in range(2):
        with transaction(db_path, immediate=True) as connection:
            grants.append(entitlement_service.create_session_in_tx(connection, grants[0].entitlement_id,
                                                                  evict_oldest=False))
    # Keep real issued rights and session rows; fix only token hashes so this
    # equal-time fixture always has hash order opposite to insertion order.
    tokens = sorted(["a" * 43, "b" * 43, "c" * 43],
                    key=lambda token: hashlib.sha256(token.encode()).hexdigest(), reverse=True)
    hashes = [hashlib.sha256(token.encode()).hexdigest() for token in tokens]
    with transaction(db_path, immediate=True) as connection:
        for grant, session_hash in zip(grants, hashes, strict=True):
            connection.execute("UPDATE sessions SET session_hash=? WHERE session_hash=?",
                (session_hash, hashlib.sha256(grant.session_id.encode()).hexdigest()))
    stored = rows(db_path, "SELECT session_hash, created_at FROM sessions ORDER BY rowid")
    assert [row["session_hash"] for row in stored] == hashes == sorted(hashes, reverse=True)
    assert {row["created_at"] for row in stored} == {to_db_time(clock.now())}
    with transaction(db_path, immediate=True) as connection:
        fourth = entitlement_service.create_session_in_tx(connection, grants[0].entitlement_id,
                                                         evict_oldest=True)
    assert rows(db_path, "SELECT session_hash FROM sessions WHERE revoked_at IS NOT NULL") == [
        {"session_hash": hashes[-1]}]
    assert len(rows(db_path, "SELECT session_hash FROM sessions WHERE revoked_at IS NULL")) == 3
    with pytest.raises(BusinessError):
        entitlement_service.require_session(tokens[-1], grants[0].course_id)
    for token in [*tokens[:-1], fourth.session_id]:
        assert entitlement_service.require_session(token, grants[0].course_id).entitlement_expires_at == redeemed.entitlement_expires_at
        assert progress_service.get_progress(token, grants[0].course_id) == {1: True}


def test_session_helpers_use_callers_connection_and_rollback(entitlement_service, redeemed, db_path):
    before = rows(db_path, "SELECT * FROM sessions")
    with pytest.raises(RuntimeError):
        with transaction(db_path, immediate=True) as connection:
            grant = entitlement_service.create_session_in_tx(connection, redeemed.session.entitlement_id, evict_oldest=False)
            assert entitlement_service.require_session_in_tx(connection, grant.session_id, grant.course_id).entitlement_id == grant.entitlement_id
            raise RuntimeError("rollback")
    assert rows(db_path, "SELECT * FROM sessions") == before
    with closing(connect(db_path)) as connection:
        with pytest.raises(BusinessError):
            entitlement_service.create_session_in_tx(connection, redeemed.session.entitlement_id, evict_oldest=False)
        with pytest.raises(BusinessError):
            entitlement_service.require_session_in_tx(connection, redeemed.session.session_id, redeemed.session.course_id)


def test_legacy_pending_session_retains_expiry_without_fabricated_policy(entitlement_service,
        active_product, db_path, clock):
    token = "l" * 43
    deadline = clock.now() + timedelta(hours=2)
    with transaction(db_path) as connection:
        connection.execute("""INSERT INTO entitlements (id, course_id, course_version, expires_at, legacy_state)
            VALUES (1, 'fixture-course', '0.1.0', ?, 'pending_verification')""", (to_db_time(deadline),))
        connection.execute("""INSERT INTO sessions (session_hash, course_id, entitlement_id, created_at, expires_at)
            VALUES (?, 'fixture-course', 1, ?, ?)""", (hashlib.sha256(token.encode()).hexdigest(), to_db_time(clock.now()), to_db_time(deadline)))
    session = entitlement_service.require_session(token, "fixture-course")
    assert session.issued_policy is None and session.csrf_hash is None
    assert session.entitlement_expires_at == session.session_expires_at == deadline
    assert rows(db_path, "SELECT * FROM recovery_credentials") == []
    assert rows(db_path, "SELECT issued_policy_json FROM entitlements") == [{"issued_policy_json": None}]
    clock.advance(hours=2)
    with pytest.raises(BusinessError):
        entitlement_service.require_session(token, "fixture-course")


def test_redemption_competes_with_batch_replacement(entitlement_service, code_service, active_product,
        actor, db_path, clock):
    batch = code_service.issue_batch(actor, BatchInput(product_id=1, count=2, purpose="sale"), "batch")
    barrier = Barrier(2)

    def redeem():
        barrier.wait(timeout=5)
        try:
            return entitlement_service.redeem(batch.codes[0].raw_code, expected_course_id=None, request_id="race")
        except BusinessError as error:
            return error

    def replace():
        barrier.wait(timeout=5)
        try:
            return CodeService(db_path, clock=clock.now).replace_unused(actor, batch.batch_id, 1, "replace", "replace")
        except BusinessError as error:
            return error

    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(redeem), pool.submit(replace)
        redemption, replacement = a.result(timeout=10), b.result(timeout=10)
    assert sum(isinstance(result, BusinessError) for result in (redemption, replacement)) == 1
    original = rows(db_path, "SELECT used_at, voided_at FROM access_codes WHERE id=1")[0]
    assert bool(original["used_at"]) != bool(original["voided_at"])
    assert rows(db_path, "SELECT revision FROM code_batches WHERE id=1") == [{"revision": 2}]
    assert len(rows(db_path, "SELECT * FROM entitlements")) == (0 if isinstance(redemption, BusinessError) else 1)


def test_revoke_is_idempotent_and_preserves_progress(entitlement_service, progress_service, redeemed,
        actor, db_path, clock):
    session = redeemed.session
    progress_service.set_completed(session.session_id, session.course_id, 1, True)
    before = rows(db_path, "SELECT * FROM entitlement_progress")
    entitlement_service.revoke(actor, session.entitlement_id, 1, "撤销", "revoke-key")
    entitlement_service.revoke(actor, session.entitlement_id, 1, "  撤销  ", "revoke-key")
    entitlement = rows(db_path, "SELECT * FROM entitlements")[0]
    assert entitlement["revision"] == 2 and entitlement["revoked_at"] == to_db_time(clock.now())
    assert entitlement["expires_at"] == to_db_time(redeemed.entitlement_expires_at)
    assert rows(db_path, "SELECT * FROM entitlement_progress") == before
    assert rows(db_path, "SELECT revoked_at FROM sessions") == [{"revoked_at": to_db_time(clock.now())}]
    assert rows(db_path, "SELECT revoked_at FROM recovery_credentials") == [{"revoked_at": to_db_time(clock.now())}]
    request = rows(db_path, "SELECT * FROM operation_requests WHERE action='entitlement.revoke'")
    assert len(request) == 1 and request[0]["idempotency_key"] == hashlib.sha256(b"revoke-key").hexdigest()
    assert len(rows(db_path, "SELECT id FROM admin_events WHERE action='entitlement.revoke' AND outcome='success'")) == 1
    with pytest.raises(BusinessError) as caught:
        entitlement_service.revoke(actor, session.entitlement_id, 1, "different", "revoke-key")
    assert caught.value.code == "idempotency_conflict"


def test_fresh_revoke_of_revoked_entitlement_is_denied_before_saving_request(entitlement_service,
        redeemed, actor, db_path):
    entitlement_service.revoke(actor, redeemed.session.entitlement_id, 1, "withdraw", "first")
    before = rows(db_path, "SELECT * FROM operation_requests")
    with pytest.raises(BusinessError) as caught:
        entitlement_service.revoke(actor, redeemed.session.entitlement_id, 2, "withdraw", "fresh")
    assert caught.value.code == "entitlement_revoked"
    assert rows(db_path, "SELECT * FROM operation_requests") == before


@pytest.mark.parametrize("fault", ["owner", "revision", "credential", "audit"])
def test_revoke_denial_or_audit_fault_keeps_grants(entitlement_service, redeemed, actor, db_path, fault):
    before = {table: rows(db_path, f"SELECT * FROM {table}") for table in ("entitlements", "sessions", "recovery_credentials", "operation_requests")}
    if fault == "audit":
        with transaction(db_path) as connection:
            connection.execute("""CREATE TRIGGER fail_revoke BEFORE INSERT ON admin_events
                WHEN NEW.action='entitlement.revoke' AND NEW.outcome='success'
                BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END""")
    with pytest.raises(BusinessError):
        entitlement_service.revoke(Actor(999, "invalid") if fault == "owner" else actor,
            redeemed.session.entitlement_id, 2 if fault == "revision" else 1,
            redeemed.session.session_id if fault == "credential" else "reason", "revoke-key")
    for table, state in before.items():
        assert rows(db_path, f"SELECT * FROM {table}") == state
    assert len(rows(db_path, "SELECT * FROM admin_events WHERE action='entitlement.revoke' AND outcome='denied'")) == 1


def test_revoke_in_tx_rolls_back_with_caller(entitlement_service, redeemed, actor, db_path):
    before = {table: rows(db_path, f"SELECT * FROM {table}") for table in ("entitlements", "sessions", "recovery_credentials", "admin_events")}
    with pytest.raises(RuntimeError):
        with transaction(db_path, immediate=True) as connection:
            entitlement_service.revoke_in_tx(connection, actor, redeemed.session.entitlement_id, "refund")
            with pytest.raises(BusinessError):
                entitlement_service.create_session_in_tx(connection, redeemed.session.entitlement_id, evict_oldest=False)
            raise RuntimeError("caller rollback")
    for table, state in before.items():
        assert rows(db_path, f"SELECT * FROM {table}") == state


@pytest.mark.parametrize("table", ["entitlements", "sessions", "recovery_credentials", "admin_events"])
def test_revoke_in_tx_storage_fault_is_generic_and_caller_rolls_back(entitlement_service,
        progress_service, redeemed, actor, db_path, table):
    session = redeemed.session
    progress_service.set_completed(session.session_id, session.course_id, 1, True)
    before = {name: rows(db_path, f"SELECT * FROM {name}") for name in
              ("entitlements", "sessions", "recovery_credentials", "entitlement_progress", "operation_requests", "admin_events")}
    operation = "INSERT" if table == "admin_events" else "UPDATE"
    with transaction(db_path) as connection:
        connection.execute(f"CREATE TRIGGER fail_shared_revoke BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'private-order'); END")
    with pytest.raises(BusinessError) as caught:
        with transaction(db_path, immediate=True) as connection:
            entitlement_service.revoke_in_tx(connection, actor, session.entitlement_id, "refund")
    assert "private-order" not in str(caught.value)
    for name, state in before.items():
        assert rows(db_path, f"SELECT * FROM {name}") == state


def test_denial_audit_fault_never_exposes_raw_database_messages(entitlement_service, issued_code, db_path):
    with transaction(db_path) as connection:
        connection.execute("""CREATE TRIGGER fail_denial BEFORE INSERT ON admin_events
            WHEN NEW.action='code.redeem' AND NEW.outcome='denied'
            BEGIN SELECT RAISE(ABORT, 'private-order'); END""")
    with pytest.raises(BusinessError) as caught:
        entitlement_service.redeem(issued_code.raw_code, expected_course_id="other", request_id=issued_code.raw_code)
    assert "private-order" not in str(caught.value) and issued_code.raw_code not in str(caught.value)
    assert rows(db_path, "SELECT used_at FROM access_codes") == [{"used_at": None}]
    assert rows(db_path, "SELECT * FROM entitlements") == []
