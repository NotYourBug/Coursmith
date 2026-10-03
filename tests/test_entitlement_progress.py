"""Shared entitlement progress and writer-serialized authorization.

Catch browser ownership, truthy input coercion and writes after revocation.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event

import pytest

from course_platform.database import open_readonly, transaction
from course_platform.delivery.entitlements import EntitlementService
from course_platform.delivery.progress import ProgressService
from course_platform.domain import BusinessError


def rows(path, sql):
    with closing(open_readonly(path)) as connection:
        return [dict(row) for row in connection.execute(sql)]


def test_completed_value_is_a_real_bool(entitlement_service, progress_service, issued_code):
    receipt = entitlement_service.redeem(issued_code.raw_code, expected_course_id=None, request_id="redeem-1")
    token, course = receipt.session.session_id, receipt.session.course_id
    progress_service.set_completed(token, course, 1, True)
    assert progress_service.get_progress(token, course) == {1: True}
    with pytest.raises(BusinessError):
        progress_service.set_completed(token, course, 1, "false")
    assert progress_service.get_progress(token, course) == {1: True}
    progress_service.set_completed(token, course, 1, False)
    assert progress_service.get_progress(token, course) == {1: False}


@pytest.mark.parametrize("chapter,completed", [(True, True), (False, True), (1.0, True), ("1", True),
    (0, True), (-1, True), (2, True), (1, 1), (1, 0), (1, None), (1, "false")])
def test_invalid_progress_leaves_no_row(progress_service, redeemed, db_path, chapter, completed):
    with pytest.raises(BusinessError):
        progress_service.set_completed(redeemed.session.session_id, redeemed.session.course_id, chapter, completed)
    assert rows(db_path, "SELECT * FROM entitlement_progress") == []


def test_progress_is_owned_by_entitlement_not_browser(entitlement_service, progress_service, redeemed, db_path):
    session = redeemed.session
    progress_service.set_completed(session.session_id, session.course_id, 1, True)
    with transaction(db_path, immediate=True) as connection:
        second = entitlement_service.create_session_in_tx(connection, session.entitlement_id, evict_oldest=False)
    restarted = ProgressService(db_path, entitlement_service=EntitlementService(db_path, clock=entitlement_service.clock), clock=entitlement_service.clock)
    assert restarted.get_progress(second.session_id, second.course_id) == {1: True}
    restarted.set_completed(second.session_id, second.course_id, 1, False)
    assert progress_service.get_progress(session.session_id, session.course_id) == {1: False}
    assert rows(db_path, "SELECT entitlement_id, completed FROM entitlement_progress") == [{"entitlement_id": session.entitlement_id, "completed": 0}]
    assert rows(db_path, "SELECT * FROM progress") == []


def test_progress_isolated_between_entitlements(entitlement_service, progress_service, redeemed,
        code_service, active_product, actor, db_path):
    from course_platform.operations.codes import BatchInput

    code = code_service.issue_batch(actor, BatchInput(product_id=1, purpose="sale"), "another").codes[0]
    second = entitlement_service.redeem(code.raw_code, expected_course_id=None, request_id="another").session
    first = redeemed.session
    progress_service.set_completed(first.session_id, first.course_id, 1, True)
    assert progress_service.get_progress(second.session_id, second.course_id) == {}
    with pytest.raises(BusinessError):
        progress_service.set_completed(first.session_id, "other", 1, True)
    assert len(rows(db_path, "SELECT * FROM entitlement_progress")) == 1


def test_revoke_then_progress_write_is_denied_without_new_progress(entitlement_service,
        progress_service, redeemed, actor, db_path):
    session = redeemed.session
    started = Event()

    def write():
        started.set()
        try:
            progress_service.set_completed(session.session_id, session.course_id, 1, True)
        except BusinessError as error:
            return error

    # One real writer owns revocation while another connection attempts progress.
    with ThreadPoolExecutor(1) as pool:
        with transaction(db_path, immediate=True) as connection:
            entitlement_service.revoke_in_tx(connection, actor, session.entitlement_id, "withdraw")
            future = pool.submit(write)
            assert started.wait(timeout=5)
        assert isinstance(future.result(timeout=10), BusinessError)
    assert rows(db_path, "SELECT * FROM entitlement_progress") == []
    with pytest.raises(BusinessError):
        progress_service.get_progress(session.session_id, session.course_id)


def test_progress_fault_preserves_previous_completion(progress_service, redeemed, db_path):
    session = redeemed.session
    progress_service.set_completed(session.session_id, session.course_id, 1, True)
    before = rows(db_path, "SELECT * FROM entitlement_progress")
    with transaction(db_path) as connection:
        connection.execute("""CREATE TRIGGER fail_progress BEFORE UPDATE ON entitlement_progress
            BEGIN SELECT RAISE(ABORT, 'storage unavailable'); END""")
    with pytest.raises(BusinessError):
        progress_service.set_completed(session.session_id, session.course_id, 1, False)
    assert rows(db_path, "SELECT * FROM entitlement_progress") == before
