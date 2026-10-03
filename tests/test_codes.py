"""One-time issuance, transaction rollback and immutable replacement promises."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta

import pytest
from pydantic import ValidationError

from course_platform.database import open_readonly, transaction
from course_platform.domain import Actor, BusinessError
from course_platform.operations.codes import BatchInput, CodeService
from course_platform.operations.products import SalesChecklist


def rows(path, sql, args=()):
    with closing(open_readonly(path)) as connection:
        return [dict(row) for row in connection.execute(sql, args)]


def issue(service, product, key="batch-key-1", **kwargs):
    return service.issue_batch(Actor(1, "issue-request"), BatchInput(product_id=product.id, purpose="sale", **kwargs), key)


def test_issue_replay_never_returns_plaintext_twice(code_service, active_product, actor, db_path, clock, caplog, tmp_path):
    data = BatchInput(product_id=active_product.id, purpose="sale", count=200)
    first = code_service.issue_batch(actor, data, "batch-key-1")
    replay = code_service.issue_batch(actor, data, "batch-key-1")
    assert len(first.codes) == len({c.raw_code for c in first.codes}) == 200
    assert replay.batch_id == first.batch_id and replay.replayed and replay.codes == ()
    assert len(rows(db_path, "SELECT id FROM code_batches")) == 1
    dump = "".join(db_path.read_bytes().decode("latin1")) + caplog.text + repr(first)
    for code in first.codes:
        assert code.raw_code.startswith("CS-") and len(code.raw_code) == 35
        assert code.expires_at == clock.now() + timedelta(days=30)
        assert code.raw_code not in dump
        assert not any(code.raw_code.encode() in p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())
    stored = rows(db_path, "SELECT code_hash, issued_policy_json FROM access_codes")
    assert stored[0]["code_hash"] == hashlib.sha256(first.codes[0].raw_code.encode()).hexdigest()
    policy = json.loads(stored[0]["issued_policy_json"])
    assert policy["title"] == "可售课程" and policy["access"]["access_days"] == 30
    assert rows(db_path, "SELECT id FROM entitlements") == []
    with pytest.raises(BusinessError) as caught:
        code_service.issue_batch(actor, data.model_copy(update={"count": 1}), "batch-key-1")
    assert caught.value.status_code == 409


@pytest.mark.parametrize("count", [1, 200])
@pytest.mark.parametrize("days", [1, 365])
def test_issue_boundaries(code_service, active_product, count, days, clock):
    receipt = issue(code_service, active_product, count=count, activation_days=days)
    assert len(receipt.codes) == count
    assert receipt.codes[0].expires_at == clock.now() + timedelta(days=days)


@pytest.mark.parametrize("field,value", [("count", 0), ("count", 201), ("count", True), ("count", 1.5),
    ("activation_days", 0), ("activation_days", 366), ("activation_days", True), ("activation_days", 1.5),
    ("product_id", True), ("purpose", "other"), ("note", "x" * 1001)])
def test_batch_input_strict_limits(field, value):
    with pytest.raises(ValidationError):
        BatchInput(**({"product_id": 1, "purpose": "sale"} | {field: value}))


def test_issue_audit_failure_leaves_no_codes(code_service, active_product, actor, db_path):
    with transaction(db_path) as connection:
        connection.execute("""CREATE TRIGGER fail_issue BEFORE INSERT ON admin_events
            WHEN NEW.action='code.issue' AND NEW.outcome='success'
            BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END""")
    with pytest.raises(BusinessError):
        issue(code_service, active_product)
    for table in ("code_batches", "access_codes", "operation_requests"):
        assert rows(db_path, f"SELECT id FROM {table}") == []
    assert len(rows(db_path, "SELECT id FROM admin_events WHERE action='code.issue' AND outcome='denied'")) == 1


def test_concurrent_issue_replay(code_service, active_product, actor, db_path):
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: issue(code_service, active_product), range(2)))
    assert sorted(len(result.codes) for result in results) == [0, 1]
    assert results[0].batch_id == results[1].batch_id
    assert len(rows(db_path, "SELECT id FROM access_codes")) == 1


def test_replacement_inherits_snapshot_and_replay_precedes_revision(code_service, product_service, active_product, actor, db_path):
    first = issue(code_service, active_product)
    old = rows(db_path, "SELECT id, issued_policy_json FROM access_codes")[0]
    data = active_product.data.model_copy(update={"title": "新承诺", "support_text": "新支持",
        "policy": active_product.data.policy.model_copy(update={"access_days": 7})})
    changed = product_service.update(actor, active_product.id, active_product.revision, data)
    product_service.activate(actor, changed.id, changed.revision,
        SalesChecklist(quality=True, sources=True, ai=True, mobile=True, downloads=True))
    replaced = code_service.replace(actor, old["id"], 1, "丢失", "replace-key")
    replay = code_service.replace(actor, old["id"], 1, "丢失", "replace-key")
    assert replaced.codes[0].raw_code != first.codes[0].raw_code
    assert replay.replayed and replay.codes == () and replay.batch_id == replaced.batch_id
    codes = rows(db_path, "SELECT id, revision, voided_at, replaces_code_id, issued_policy_json FROM access_codes ORDER BY id")
    assert codes[0]["voided_at"] and codes[0]["revision"] == 2
    assert codes[1]["replaces_code_id"] == old["id"] and codes[1]["issued_policy_json"] == old["issued_policy_json"]
    assert rows(db_path, "SELECT revision FROM code_batches WHERE id=?", (first.batch_id,))[0]["revision"] == 2
    with pytest.raises(BusinessError) as caught:
        code_service.replace(actor, old["id"], 1, "不同原因", "replace-key")
    assert caught.value.status_code == 409


def test_batch_replace_keeps_redeemed_items(code_service, active_product, actor, db_path, clock):
    first = issue(code_service, active_product, count=3)
    # Task7 MUST replace this direct used_at simulation with real redemption,
    # then recheck entitlement/credential/session/progress preservation.
    with transaction(db_path) as connection:
        connection.execute("UPDATE access_codes SET used_at=? WHERE id=1", (clock.now().isoformat(),))
    receipt = code_service.replace_unused(actor, first.batch_id, 1, "明文丢失", "batch-replace")
    assert len(receipt.codes) == 2
    assert code_service.replace_unused(actor, first.batch_id, 1, "明文丢失", "batch-replace").codes == ()
    original = rows(db_path, "SELECT id, used_at, voided_at, revision FROM access_codes WHERE batch_id=? ORDER BY id", (first.batch_id,))
    assert original[0]["used_at"] and original[0]["voided_at"] is None and original[0]["revision"] == 1
    assert all(row["voided_at"] and row["revision"] == 2 for row in original[1:])
    assert {row["replaces_code_id"] for row in rows(db_path, "SELECT replaces_code_id FROM access_codes WHERE batch_id=?", (receipt.batch_id,))} == {2, 3}


def test_revoke_unused_and_stale_revision(code_service, active_product, actor, db_path, clock):
    batch = issue(code_service, active_product, count=3)
    with transaction(db_path) as connection:
        connection.execute("UPDATE access_codes SET used_at=? WHERE id=1", (clock.now().isoformat(),))
    with pytest.raises(BusinessError):
        code_service.revoke(actor, 1, 1, "不能撤兑换")
    with pytest.raises(BusinessError):
        code_service.revoke(actor, 2, 9, "陈旧")
    code_service.revoke(actor, 2, 1, "作废")
    with pytest.raises(BusinessError):
        code_service.revoke_unused(actor, batch.batch_id, 1, "陈旧批次")
    assert code_service.revoke_unused(actor, batch.batch_id, 2, "作废剩余") == 1
    state = rows(db_path, "SELECT used_at, voided_at FROM access_codes ORDER BY id")
    assert state[0]["used_at"] and state[0]["voided_at"] is None
    assert state[1]["voided_at"] and state[2]["voided_at"]


@pytest.mark.parametrize("fault", ["paused", "approval", "content", "snapshot"])
def test_replacement_refuses_unfulfillable_sale_without_voiding(code_service, active_product, actor, db_path, fixture_package, fault):
    batch = issue(code_service, active_product)
    with transaction(db_path) as connection:
        if fault == "paused":
            connection.execute("UPDATE products SET status='paused'")
        elif fault == "approval":
            connection.execute("UPDATE products SET sales_check_json=NULL")
        elif fault == "snapshot":
            connection.execute("UPDATE access_codes SET issued_policy_json=NULL")
    if fault == "content":
        (fixture_package / "chapters/01.html").write_text("changed", encoding="utf8")
    with pytest.raises(BusinessError):
        code_service.replace_unused(actor, batch.batch_id, 1, "补发", "replace-key")
    assert rows(db_path, "SELECT voided_at FROM access_codes")[0]["voided_at"] is None
    assert len(rows(db_path, "SELECT id FROM code_batches")) == 1


def test_in_tx_keeps_callers_snapshot_and_rollback(code_service, active_product, actor, product_service, db_path):
    with pytest.raises(RuntimeError):
        with transaction(db_path, immediate=True) as connection:
            policy = product_service.require_sale_ready_in_tx(connection, active_product.id)
            receipt = code_service.issue_in_tx(connection, actor, policy, BatchInput(product_id=active_product.id, purpose="gift"),
                order_id=None, idempotency_key="in-tx")
            assert len(receipt.codes) == 1 and connection.in_transaction
            raise RuntimeError("caller rollback")
    assert rows(db_path, "SELECT id FROM access_codes") == []


def test_owner_and_input_denials_are_safe(code_service, active_product, actor, db_path):
    with pytest.raises(BusinessError):
        code_service.issue_batch(Actor(999, "missing-owner"), BatchInput(product_id=active_product.id, purpose="test"), "key")
    with pytest.raises(BusinessError):
        code_service.issue_batch(actor, BatchInput.model_construct(product_id=1, purpose="test", count=True), "key")
    assert rows(db_path, "SELECT id FROM access_codes") == []
    denied = rows(db_path, "SELECT actor_admin_id, request_id, changes_json FROM admin_events WHERE action='code.issue' AND outcome='denied'")
    assert denied[0]["actor_admin_id"] is None and denied[0]["request_id"] == "missing-owner"
    assert len(denied) == 2


@pytest.mark.parametrize("operation,action", [("revoke", "code.void"), ("revoke_unused", "code.void"),
    ("replace", "code.reissue"), ("replace_unused", "code.reissue")])
def test_mutation_audit_failure_rolls_back_every_link(code_service, active_product, actor, db_path, operation, action):
    batch = issue(code_service, active_product, count=2)
    before = rows(db_path, "SELECT id, voided_at, revision, replaces_code_id FROM access_codes")
    with transaction(db_path) as connection:
        connection.execute(f"""CREATE TRIGGER fail_mutation BEFORE INSERT ON admin_events
            WHEN NEW.action='{action}' AND NEW.outcome='success'
            BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END""")
    args = [actor, batch.batch_id if operation.endswith("unused") else 1, 1, "原因"]
    if operation.startswith("replace"):
        args.append("mutation-key")
    with pytest.raises(BusinessError):
        getattr(code_service, operation)(*args)
    assert rows(db_path, "SELECT id, voided_at, revision, replaces_code_id FROM access_codes") == before
    assert len(rows(db_path, "SELECT id FROM code_batches")) == 1
    assert rows(db_path, "SELECT revision FROM code_batches")[0]["revision"] == 1
    assert len(rows(db_path, "SELECT id FROM operation_requests")) == 1
    assert len(rows(db_path, "SELECT id FROM admin_events WHERE action=? AND outcome='denied'", (action,))) == 1


def test_issue_replay_after_pause_and_normalized_requests(code_service, active_product, actor, db_path):
    first = issue(code_service, active_product, note="说明")
    with transaction(db_path) as connection:
        connection.execute("UPDATE products SET status='paused'")
    replay = issue(code_service, active_product, note="  说明  ")
    assert replay.replayed and replay.codes == () and replay.batch_id == first.batch_id
    with pytest.raises(BusinessError) as caught:
        issue(code_service, active_product, "fresh-key")
    assert caught.value.code == "product_not_active"


def test_issue_in_tx_uses_supplied_original_policy_after_edit(code_service, active_product, product_service, actor, db_path):
    with transaction(db_path, immediate=True) as connection:
        original = product_service.require_sale_ready_in_tx(connection, active_product.id)
    data = active_product.data.model_copy(update={"title": "新名称", "policy": active_product.data.policy.model_copy(update={"access_days": 3})})
    changed = product_service.update(actor, active_product.id, active_product.revision, data)
    product_service.activate(actor, changed.id, changed.revision,
        SalesChecklist(quality=True, sources=True, ai=True, mobile=True, downloads=True))
    with transaction(db_path, immediate=True) as connection:
        connection.execute("""INSERT INTO orders
            (id, product_id, channel, shop, external_order_id, amount_cents, created_by, issued_policy_json)
            VALUES (1, 1, 'store', 'shop', 'order-one', 0, 1, ?)""", (original.model_dump_json(),))
        receipt = code_service.issue_in_tx(connection, actor, original, BatchInput(product_id=1, purpose="sale"), order_id=1, idempotency_key="order-issue")
        assert len(receipt.codes) == 1
    stored = rows(db_path, "SELECT order_id, issued_policy_json FROM access_codes")[0]
    assert stored["order_id"] == 1 and json.loads(stored["issued_policy_json"])["access"]["access_days"] == 30
    assert json.loads(stored["issued_policy_json"])["title"] == "可售课程"


def test_expired_unused_replacement_gets_new_activation_deadline(code_service, active_product, actor, clock):
    original = issue(code_service, active_product)
    clock.advance(days=31)
    replacement = code_service.replace(actor, 1, 1, "未使用且已过期", "renew-activation")
    assert replacement.codes[0].expires_at == clock.now() + timedelta(days=30)
    assert replacement.codes[0].expires_at > original.codes[0].expires_at


def test_direct_invalid_identifier_denial_is_safe(code_service, active_product, actor, db_path):
    with pytest.raises(BusinessError) as caught:
        code_service.revoke(actor, "CS-untrusted", 1, "原因")
    assert caught.value.code == "invalid_code"
    denied = rows(db_path, "SELECT object_id, changes_json FROM admin_events WHERE action='code.void' AND outcome='denied'")
    assert denied == [{"object_id": "invalid", "changes_json": '{"error_code":"invalid_code"}'}]
