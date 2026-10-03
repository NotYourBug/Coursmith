"""Real order transactions, original promises and paid ownership provenance."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

import pytest
from pydantic import ValidationError

from course_platform.database import transaction
from course_platform.domain import BusinessError
from course_platform.operations.codes import BatchInput
from course_platform.operations.orders import OrderInput, delivery_text
from course_platform.operations.products import SalesChecklist


def data(product, clock, **changes):
    return OrderInput(channel="taobao", shop_id="shop-1", external_order_id="private-order-1",
        product_id=product.id, paid_cents=0, paid_at=clock.now(), note="已核验店铺付款", **changes)


def sale(order_service, active_product, actor, clock):
    order = order_service.record(actor, data(active_product, clock), "record")
    receipt = order_service.issue(actor, order.id, order.revision, "issue")
    return order, receipt


def test_order_identity_and_issue_are_unique(order_service, active_product, actor, clock, db_path):
    order, receipt = sale(order_service, active_product, actor, clock)
    assert order.status == "recorded" and order.data.paid_cents == 0
    assert order_service.record(actor, data(active_product, clock), "record").id == order.id
    with pytest.raises(BusinessError) as err:
        order_service.record(actor, data(active_product, clock), "different-record")
    assert err.value.status_code == 409
    replay = order_service.issue(actor, order.id, order.revision, "issue")
    assert replay.batch_id == receipt.batch_id and replay.replayed and not replay.codes
    with pytest.raises(BusinessError) as err:
        order_service.issue(actor, order.id, 2, "another-issue")
    assert err.value.status_code == 409
    with transaction(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM access_codes").fetchone()[0] == 1
        assert tuple(connection.execute("SELECT status, delivery_state, paid_at FROM orders").fetchone()) == (
            "paid", "code_ready", "2026-10-02T00:00:00+00:00")
        assert "private-order-1" not in str([tuple(r) for r in connection.execute("SELECT * FROM admin_events")])
    assert receipt.codes[0].raw_code.encode() not in db_path.read_bytes()


@pytest.mark.parametrize("amount", [-1, True, 1.2])
def test_money_is_strict_nonnegative(active_product, clock, amount):
    values = data(active_product, clock).model_dump() | {"paid_cents": amount}
    with pytest.raises(ValidationError):
        OrderInput(**values)


def test_confirm_delivery_after_activation_does_not_downgrade(order_service, active_product,
        actor, clock, entitlement_service, db_path):
    order, receipt = sale(order_service, active_product, actor, clock)
    entitlement_service.redeem(receipt.codes[0].raw_code, expected_course_id=None, request_id="redeem")
    activated = order_service.get_order(order.id)
    assert activated.status == "activated"
    clock.advance(hours=1)
    result = order_service.confirm_delivery(actor, order.id, activated.revision, "confirm")
    assert result.status == "activated"
    assert order_service.confirm_delivery(actor, order.id, activated.revision, "confirm").id == order.id
    with transaction(db_path) as connection:
        assert connection.execute("SELECT delivered_at FROM orders").fetchone()[0] == "2026-10-02T01:00:00+00:00"


def test_order_issue_keeps_recorded_policy_after_product_edit(order_service, active_product,
        actor, clock, product_service, entitlement_service, db_path):
    order = order_service.record(actor, data(active_product, clock), "record")
    changed = active_product.data.model_copy(update={"title": "新商品名",
        "policy": active_product.data.policy.model_copy(update={"access_days": 7})})
    updated = product_service.update(actor, active_product.id, active_product.revision, changed)
    product_service.activate(actor, updated.id, updated.revision,
        SalesChecklist(quality=True, sources=True, ai=True, mobile=True, downloads=True))
    receipt = order_service.issue(actor, order.id, order.revision, "issue")
    redeemed = entitlement_service.redeem(receipt.codes[0].raw_code, expected_course_id=None, request_id="redeem")
    assert (redeemed.entitlement_expires_at - clock.now()).days == 30
    with transaction(db_path) as connection:
        snapshots = [connection.execute(f"SELECT issued_policy_json FROM {table}").fetchone()[0]
            for table in ("orders", "code_batches", "access_codes", "entitlements")]
        assert len(set(snapshots)) == 1 and "可售课程" in snapshots[0] and "新商品名" not in snapshots[0]


def test_refund_and_redeem_race_leaves_no_live_access(order_service, active_product, actor,
        clock, entitlement_service, recovery_service, db_path):
    order, receipt = sale(order_service, active_product, actor, clock)
    barrier = Barrier(2)
    def redeem():
        barrier.wait()
        try:
            return entitlement_service.redeem(receipt.codes[0].raw_code, expected_course_id=None, request_id="race")
        except BusinessError as error:
            assert error.code == "redemption_denied"
    def refund():
        barrier.wait()
        try:
            return order_service.record_refund(actor, order.id, 2, "已核验店铺完成退款", "refund")
        except BusinessError as error:
            assert error.code == "stale_revision"
            current = order_service.get_order(order.id)
            return order_service.record_refund(actor, order.id, current.revision, "已核验店铺完成退款", "refund")
    with ThreadPoolExecutor(max_workers=2) as pool:
        buyer, refunded = pool.submit(redeem), pool.submit(refund)
        buyer, refunded = buyer.result(), refunded.result()
    assert refunded.status == "refunded"
    if buyer:
        with pytest.raises(BusinessError):
            entitlement_service.require_session(buyer.session.session_id, buyer.session.course_id)
        with pytest.raises(BusinessError):
            recovery_service.restore(buyer.raw_recovery_key, request_id="restore")
    with transaction(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM access_codes WHERE used_at IS NULL AND voided_at IS NULL").fetchone()[0] == 0
        for table in ("entitlements", "sessions", "recovery_credentials"):
            assert connection.execute(f"SELECT count(*) FROM {table} WHERE revoked_at IS NULL").fetchone()[0] == 0


def test_refund_audit_failure_rolls_back_revocation(order_service, active_product, actor,
        clock, entitlement_service, db_path):
    order, receipt = sale(order_service, active_product, actor, clock)
    redeemed = entitlement_service.redeem(receipt.codes[0].raw_code, expected_course_id=None, request_id="redeem")
    with transaction(db_path) as connection:
        connection.execute("""CREATE TRIGGER fail_refund BEFORE INSERT ON admin_events
            WHEN NEW.action='order.refund' AND NEW.outcome='success'
            BEGIN SELECT RAISE(ABORT, 'audit failure'); END""")
    with pytest.raises(BusinessError):
        order_service.record_refund(actor, order.id, 3, "已核验退款", "refund")
    assert entitlement_service.require_session(redeemed.session.session_id, redeemed.session.course_id)
    with transaction(db_path) as connection:
        assert tuple(connection.execute("SELECT status, revision FROM orders").fetchone()) == ("paid", 3)
        assert connection.execute("SELECT count(*) FROM operation_requests WHERE action='order.refund'").fetchone()[0] == 0
        assert connection.execute("SELECT revoked_at FROM recovery_credentials").fetchone()[0] is None
        assert connection.execute("SELECT count(*) FROM admin_events WHERE action='order.refund' AND outcome='denied'").fetchone()[0] == 1


def test_attach_redeemed_code_keeps_one_entitlement(order_service, active_product, actor,
        clock, code_service, entitlement_service, recovery_service, db_path):
    code = code_service.issue_batch(actor, BatchInput(product_id=active_product.id, purpose="sale"), "unbound").codes[0]
    redeemed = entitlement_service.redeem(code.raw_code, expected_course_id=None, request_id="unbound-redeem")
    order = order_service.record(actor, data(active_product, clock), "record")
    attached = order_service.attach_code(actor, order.id, order.revision, code.public_id, "核验此码对应店铺付款", "attach")
    assert attached.status == "activated"
    assert order_service.attach_code(actor, order.id, order.revision, code.public_id, "核验此码对应店铺付款", "attach").id == order.id
    with transaction(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM entitlements").fetchone()[0] == 1
        right = connection.execute("SELECT id, order_id, revision, verified_by, verified_reason FROM entitlements").fetchone()
        assert right["id"] == redeemed.session.entitlement_id and right["order_id"] == order.id
        assert right["verified_by"] == actor.admin_id and right["verified_reason"] == "核验此码对应店铺付款"
    assert recovery_service.reset(actor, right["id"], right["revision"], "核验店铺凭证遗失", "reset").raw_key


def test_real_sale_produces_verified_ownership_for_reset_and_progress(order_service, active_product,
        actor, clock, entitlement_service, recovery_service, progress_service, db_path):
    order, issued = sale(order_service, active_product, actor, clock)
    buyer = entitlement_service.redeem(issued.codes[0].raw_code, expected_course_id=None, request_id="buyer")
    progress_service.set_completed(buyer.session.session_id, buyer.session.course_id, 1, True)
    with transaction(db_path) as connection:
        right = connection.execute("SELECT * FROM entitlements").fetchone()
        source = connection.execute("SELECT verified_at, verified_by, verified_reason FROM access_codes").fetchone()
        assert right["order_id"] == order.id
        assert tuple(source) == (right["verified_at"], actor.admin_id, right["verified_reason"])
        assert right["verified_reason"]
    clock.advance(hours=1)
    reset = recovery_service.reset(actor, buyer.session.entitlement_id, right["revision"], "核验买家凭证遗失", "reset")
    with pytest.raises(BusinessError):
        entitlement_service.require_session(buyer.session.session_id, buyer.session.course_id)
    with pytest.raises(BusinessError):
        recovery_service.restore(buyer.raw_recovery_key, request_id="old-key")
    restored = recovery_service.restore(reset.raw_key, request_id="new-key")
    assert progress_service.get_progress(restored.session_id, restored.course_id) == {1: True}
    with transaction(db_path) as connection:
        assert connection.execute("SELECT expires_at FROM entitlements").fetchone()[0] == right["expires_at"]


def test_delivery_copy_distinguishes_shanghai_activation_and_access_dates(order_service,
        active_product, actor, clock):
    order, issued = sale(order_service, active_product, actor, clock)
    policy = order_service.get_policy(order.id)
    text = delivery_text(policy, issued.codes[0], "https://courses.example")
    assert "可售课程" in text and "https://courses.example" in text and issued.codes[0].raw_code in text
    assert "2026-11-01 08:00:00 Asia/Shanghai" in text and "激活截止" in text
    assert "学习期限" in text and "激活后30天" in text and "在线" in text and "邮件支持" in text
    no_expiry = policy.model_copy(update={"access": policy.access.model_copy(update={"access_mode": "no_fixed_expiry", "access_days": None})})
    text = delivery_text(no_expiry, issued.codes[0], "https://courses.example")
    assert "无固定到期日" in text and "永久" not in text and "自动升级" not in text


def test_replacement_sale_preserves_real_purchase_proof(order_service, active_product, actor,
        clock, code_service, entitlement_service, recovery_service, db_path):
    order, issued = sale(order_service, active_product, actor, clock)
    replacement = code_service.replace(actor, 1, 1, "发货响应遗失", "replace")
    buyer = entitlement_service.redeem(replacement.codes[0].raw_code, expected_course_id=None, request_id="replacement")
    with transaction(db_path) as connection:
        right = connection.execute("SELECT revision, verified_at, verified_by, verified_reason FROM entitlements").fetchone()
        assert right["verified_at"] and right["verified_by"] == actor.admin_id and right["verified_reason"]
    reset = recovery_service.reset(actor, buyer.session.entitlement_id, right["revision"], "核验店铺订单遗失凭证", "reset-replacement")
    assert reset.raw_key
    assert order_service.get_order(order.id).status == "activated"


@pytest.mark.parametrize("fault", ["batch_policy", "code_creator", "entitlement_creator", "code_proof", "future_proof"])
def test_reset_rejects_inconsistent_sale_provenance(order_service, active_product, actor, clock,
        entitlement_service, recovery_service, db_path, fault):
    order, issued = sale(order_service, active_product, actor, clock)
    buyer = entitlement_service.redeem(issued.codes[0].raw_code, expected_course_id=None, request_id="sale")
    with transaction(db_path) as connection:
        if fault == "batch_policy":
            policy = order_service.get_policy(order.id).model_copy(update={"title": "不一致的批次商品"})
            connection.execute("UPDATE code_batches SET issued_policy_json=?", (policy.model_dump_json(),))
        elif fault == "code_creator":
            connection.execute("UPDATE access_codes SET created_by=NULL")
        elif fault == "entitlement_creator":
            connection.execute("UPDATE entitlements SET created_by=NULL")
        elif fault == "code_proof":
            connection.execute("UPDATE access_codes SET verified_reason='不同的核验记录'")
        else:
            connection.execute("UPDATE entitlements SET verified_at='2027-10-02T00:00:00+00:00'")
    with pytest.raises(BusinessError) as err:
        recovery_service.reset(actor, buyer.session.entitlement_id, 1, "当前调用者核验", "bad-proof-reset")
    assert err.value.status_code == 409
    assert entitlement_service.require_session(buyer.session.session_id, buyer.session.course_id)


def test_refund_closes_entire_replacement_chain_and_replay(order_service, active_product,
        actor, clock, code_service, entitlement_service, db_path):
    order, issued = sale(order_service, active_product, actor, clock)
    second = code_service.replace(actor, 1, 1, "遗失补发", "second")
    third = code_service.replace(actor, 2, 1, "再次遗失", "third")
    refunded = order_service.record_refund(actor, order.id, 2, "核验店铺完成退款", "refund")
    assert refunded.status == "refunded"
    for code in (*issued.codes, *second.codes, *third.codes):
        with pytest.raises(BusinessError):
            entitlement_service.redeem(code.raw_code, expected_course_id=None, request_id="after-refund")
    with transaction(db_path) as connection:
        before = [tuple(row) for row in connection.execute("SELECT * FROM operation_requests")]
        assert connection.execute("SELECT count(*) FROM access_codes WHERE voided_at IS NOT NULL").fetchone()[0] == 3
    assert order_service.record_refund(actor, order.id, 2, "核验店铺完成退款", "refund").revision == refunded.revision
    with transaction(db_path) as connection:
        assert before == [tuple(row) for row in connection.execute("SELECT * FROM operation_requests")]
    with pytest.raises(BusinessError) as err:
        order_service.record_refund(actor, order.id, 2, "不同原因", "refund")
    assert err.value.code == "idempotency_conflict"


@pytest.mark.parametrize("fault", ["gift", "policy", "other_order", "existing_authorization", "missing_reason"])
def test_attach_rejects_unverified_or_duplicate_associations(order_service, active_product, actor,
        clock, code_service, db_path, fault):
    order = order_service.record(actor, data(active_product, clock), "record")
    code = code_service.issue_batch(actor, BatchInput(product_id=active_product.id,
        purpose="gift" if fault == "gift" else "sale"), "unbound").codes[0]
    if fault == "policy":
        with transaction(db_path) as connection:
            policy = order_service.get_policy(order.id).model_copy(update={"title": "另一政策"})
            connection.execute("UPDATE access_codes SET issued_policy_json=?", (policy.model_dump_json(),))
    elif fault == "other_order":
        another = order_service.record(actor, data(active_product, clock).model_copy(update={"external_order_id": "another"}), "another")
        with transaction(db_path) as connection:
            connection.execute("UPDATE access_codes SET order_id=?", (another.id,))
    elif fault == "existing_authorization":
        order_service.issue(actor, order.id, 1, "existing")
    with pytest.raises(BusinessError):
        order_service.attach_code(actor, order.id, 2 if fault == "existing_authorization" else 1, code.public_id,
            "" if fault == "missing_reason" else "核验原码付款", "attach")
    with transaction(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM operation_requests WHERE action='order.attach'").fetchone()[0] == 0


@pytest.mark.parametrize("operation,action", [("record", "order.create"), ("issue", "order.issue"),
    ("confirm_delivery", "order.deliver"), ("attach_code", "order.attach")])
def test_order_audit_failure_rolls_back_all_side_effects(order_service, active_product, actor,
        clock, code_service, db_path, operation, action):
    if operation != "record":
        order = order_service.record(actor, data(active_product, clock), "record")
    if operation == "confirm_delivery":
        order_service.issue(actor, order.id, 1, "initial-issue")
    if operation == "attach_code":
        code = code_service.issue_batch(actor, BatchInput(product_id=active_product.id, purpose="sale"), "unbound").codes[0]
    with transaction(db_path) as connection:
        connection.execute(f"""CREATE TRIGGER fail_order BEFORE INSERT ON admin_events
            WHEN NEW.action='{action}' AND NEW.outcome='success'
            BEGIN SELECT RAISE(ABORT, 'audit failure'); END""")
        before = {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in ("orders", "access_codes", "code_batches", "operation_requests")}
    with pytest.raises(BusinessError):
        if operation == "record":
            order_service.record(actor, data(active_product, clock), "record")
        elif operation == "attach_code":
            order_service.attach_code(actor, order.id, 1, code.public_id, "核验关联", "attach")
        else:
            getattr(order_service, operation)(actor, order.id, 2 if operation == "confirm_delivery" else 1, "action")
    with transaction(db_path) as connection:
        assert before == {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in before}
        assert connection.execute("SELECT count(*) FROM admin_events WHERE action=? AND outcome='denied'", (action,)).fetchone()[0] == 1


def test_order_list_pagination_uses_minimal_projection(order_service, active_product, actor, clock):
    for index in range(21):
        order_service.record(actor, data(active_product, clock).model_copy(update={"external_order_id": f"private-{index}"}), f"record-{index}")
    first, total = order_service.list_orders(channel="taobao", shop_id="shop-1", status="recorded")
    second, _ = order_service.list_orders(page=2)
    assert total == 21 and len(first) == 20 and len(second) == 1
    assert [row["id"] for row in first] == list(range(21, 1, -1)) and second[0]["id"] == 1
    assert all("external_order_id" not in row and "notes" not in row and "issued_policy_json" not in row for row in first)
    with pytest.raises(BusinessError):
        order_service.list_orders(page=2**63)


def test_historical_shanghai_display_uses_actual_iana_rules():
    from course_platform.operations.orders import display_time
    assert display_time(datetime(1990, 7, 1, tzinfo=timezone.utc)) == "1990-07-01 09:00:00 Asia/Shanghai"


def test_order_paid_time_normalization_future_naive_and_replay(order_service, active_product, actor, clock):
    values = data(active_product, clock).model_dump()
    with pytest.raises(ValidationError):
        OrderInput(**(values | {"paid_at": datetime(2026, 10, 2)}))
    values["paid_at"] = datetime.fromisoformat("2026-10-02T08:00:00+08:00")
    first = order_service.record(actor, OrderInput(**values), "record")
    assert order_service.record(actor, data(active_product, clock), "record").id == first.id
    with pytest.raises(BusinessError):
        order_service.record(actor, OrderInput(**(values | {"paid_at": datetime(2027, 1, 1, tzinfo=timezone.utc)})), "future")


@pytest.mark.parametrize("fault", ["paused", "content"])
def test_order_issue_checks_current_sale_and_original_content(order_service, active_product, actor,
        clock, product_service, fixture_package, fault, db_path):
    order = order_service.record(actor, data(active_product, clock), "record")
    if fault == "paused":
        product_service.set_status(actor, active_product.id, active_product.revision, "paused")
    else:
        (fixture_package / "chapters" / "01.html").write_text("changed", encoding="utf8")
    with pytest.raises(BusinessError):
        order_service.issue(actor, order.id, 1, "issue")
    with transaction(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM access_codes").fetchone()[0] == 0


def test_attach_and_redeem_race_preserves_one_verified_right(order_service, active_product,
        actor, clock, code_service, entitlement_service, recovery_service, db_path):
    order = order_service.record(actor, data(active_product, clock), "record")
    code = code_service.issue_batch(actor, BatchInput(product_id=active_product.id, purpose="sale"), "unbound").codes[0]
    gate = Barrier(2)
    def attach():
        gate.wait()
        return order_service.attach_code(actor, order.id, 1, code.public_id, "核验店铺原码付款", "attach")
    def redeem():
        gate.wait()
        return entitlement_service.redeem(code.raw_code, expected_course_id=None, request_id="buyer")
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner, buyer = pool.submit(attach), pool.submit(redeem)
        owner.result()
        buyer = buyer.result()
    assert order_service.get_order(order.id).status == "activated"
    with transaction(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM entitlements").fetchone()[0] == 1
        row = connection.execute("SELECT revision, order_id, verified_reason FROM entitlements").fetchone()
        assert row["order_id"] == order.id and row["verified_reason"] == "核验店铺原码付款"
    assert recovery_service.reset(actor, buyer.session.entitlement_id, row["revision"], "核验凭证遗失", "reset").raw_key


def test_reset_and_refund_race_leaves_all_credentials_revoked(order_service, active_product,
        actor, clock, entitlement_service, recovery_service, db_path):
    order, issued = sale(order_service, active_product, actor, clock)
    buyer = entitlement_service.redeem(issued.codes[0].raw_code, expected_course_id=None, request_id="buyer")
    gate = Barrier(2)
    def reset():
        gate.wait()
        try:
            return recovery_service.reset(actor, buyer.session.entitlement_id, 1, "核验凭证遗失", "reset")
        except BusinessError as error:
            assert error.code == "session_denied"
    def refund():
        gate.wait()
        return order_service.record_refund(actor, order.id, 3, "核验店铺退款完成", "refund")
    with ThreadPoolExecutor(max_workers=2) as pool:
        credential, refunded = pool.submit(reset), pool.submit(refund)
        credential, refunded = credential.result(), refunded.result()
    assert refunded.status == "refunded"
    if credential:
        with pytest.raises(BusinessError):
            recovery_service.restore(credential.raw_key, request_id="after-refund")
    with transaction(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM recovery_credentials WHERE revoked_at IS NULL").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM sessions WHERE revoked_at IS NULL").fetchone()[0] == 0


def test_attach_rejects_a_fork_with_two_live_codes(order_service, active_product, actor,
        clock, code_service, db_path):
    order = order_service.record(actor, data(active_product, clock), "record")
    first = code_service.issue_batch(actor, BatchInput(product_id=active_product.id, purpose="sale"), "first").codes[0]
    code_service.issue_batch(actor, BatchInput(product_id=active_product.id, purpose="sale"), "second")
    with transaction(db_path) as connection:
        # Malformed replacement history cannot grant two usable codes to one order.
        connection.execute("UPDATE access_codes SET replaces_code_id=1 WHERE id=2")
    with pytest.raises(BusinessError):
        order_service.attach_code(actor, order.id, 1, first.public_id, "核验付款", "attach")
    assert order_service.get_order(order.id).status == "recorded"
