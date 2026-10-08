"""Compatibility adapter uses real sale-ready content and retains progress."""
import hashlib
import json
from datetime import timedelta

import pytest

from course_platform.access import AccessService, InvalidAccessCode
from course_platform.content import CourseManifest
from course_platform.database import sync_course, to_db_time, transaction
from course_platform.domain import BusinessError


@pytest.fixture
def fixture_package(fixture_package):
    data = json.loads((fixture_package / "manifest.json").read_text(encoding="utf8"))
    data["chapter_count"] = 2
    data["chapters"].append(dict(number=2, title="第二章", path="chapters/02.html", free_preview=False))
    (fixture_package / "chapters" / "02.html").write_text(
        '<!doctype html><html><head><title>第二章</title></head><body><h1>第二章</h1></body></html>', encoding="utf8")
    (fixture_package / "manifest.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf8")
    return fixture_package


@pytest.fixture
def access_service(db_path, active_product, clock):
    return AccessService(db_path, session_ttl_hours=72, clock=clock.now)


def test_redeem_code_once_and_reject_second_use(access_service, actor):
    code = access_service.create_access_code("fixture-course", actor=actor)
    receipt = access_service.redeem_access_code(code, "fixture-course")
    assert receipt.session.course_id == "fixture-course"
    assert receipt.raw_recovery_key.startswith("LK-")
    with pytest.raises(InvalidAccessCode):
        access_service.redeem_access_code(code, "fixture-course")


def test_code_cannot_be_used_for_another_course(access_service, actor):
    code = access_service.create_access_code("fixture-course", actor=actor)
    with pytest.raises(InvalidAccessCode):
        access_service.redeem_access_code(code, "ai-infra")


def test_expired_code_is_rejected(access_service, actor, clock):
    code = access_service.create_access_code("fixture-course", actor=actor)
    with transaction(access_service.database_path) as connection:
        connection.execute("UPDATE access_codes SET expires_at=? WHERE code_hash=?",
            (to_db_time(clock.now()-timedelta(minutes=1)), hashlib.sha256(code.encode()).hexdigest()))
    with pytest.raises(InvalidAccessCode):
        access_service.redeem_access_code(code, "fixture-course")


def test_progress_is_scoped_to_session_and_course(access_service, actor):
    code = access_service.create_access_code("fixture-course", actor=actor)
    session = access_service.redeem_access_code(code, "fixture-course").session
    access_service.record_progress(session.session_id, "fixture-course", 2, True)
    assert access_service.get_progress(session.session_id, "fixture-course") == {2: True}
    second = access_service.redeem_access_code(access_service.create_access_code("fixture-course", actor=actor), "fixture-course").session
    assert access_service.get_progress(second.session_id, "fixture-course") == {}
    with pytest.raises(BusinessError):
        access_service.get_progress(session.session_id, "wrong-course")


def test_resyncing_course_does_not_delete_existing_progress(access_service, actor, fixture_package):
    code = access_service.create_access_code("fixture-course", actor=actor)
    session = access_service.redeem_access_code(code, "fixture-course").session
    access_service.record_progress(session.session_id, "fixture-course", 2, True)
    manifest = CourseManifest.model_validate_json((fixture_package / "manifest.json").read_bytes())
    changed = manifest.model_copy(update={"version": "0.1.1", "chapters": [
        chapter.model_copy(update={"title": chapter.title+"（更新）"}) for chapter in manifest.chapters]})
    with pytest.raises(BusinessError):
        sync_course(changed, fixture_package, access_service.database_path)
    assert access_service.get_progress(session.session_id, "fixture-course") == {2: True}
    sync_course(manifest, fixture_package, access_service.database_path)
    assert access_service.get_progress(session.session_id, "fixture-course") == {2: True}
