from datetime import datetime, timedelta, timezone

import pytest

from course_platform.access import AccessService, InvalidAccessCode
from course_platform.content import CourseManifest, ChapterManifest
from course_platform.database import initialize_database, sync_course


@pytest.fixture
def access_service(tmp_path):
    database_path = tmp_path / "course.db"
    initialize_database(database_path)
    manifest = CourseManifest(
        course_id="agent-development",
        slug="agent-development",
        title="Agent 开发入门到实战",
        category="ai-development",
        version="0.1.0",
        status="published",
        chapter_count=2,
        chapters=[
            ChapterManifest(number=1, title="第一章", path="chapters/01.html"),
            ChapterManifest(number=2, title="第二章", path="chapters/02.html"),
        ],
        free_chapters=[1],
        contains_ai_generated_content=True,
        ai_disclosure="部分内容由人工智能辅助生成，并经过人工审核。",
        source_manifest="SOURCES.txt",
        license_file="LICENSE.txt",
    )
    sync_course(manifest, tmp_path / "agent-development", database_path)
    return AccessService(database_path, session_ttl_hours=72)


def test_redeem_code_once_and_reject_second_use(access_service):
    code = access_service.create_access_code("agent-development")
    session = access_service.redeem_access_code(code, "agent-development")

    assert session.course_id == "agent-development"
    with pytest.raises(InvalidAccessCode):
        access_service.redeem_access_code(code, "agent-development")


def test_code_cannot_be_used_for_another_course(access_service):
    code = access_service.create_access_code("agent-development")

    with pytest.raises(InvalidAccessCode):
        access_service.redeem_access_code(code, "ai-infra")


def test_expired_code_is_rejected(access_service):
    code = access_service.create_access_code(
        "agent-development",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    with pytest.raises(InvalidAccessCode):
        access_service.redeem_access_code(code, "agent-development")


def test_progress_is_scoped_to_session_and_course(access_service):
    code = access_service.create_access_code("agent-development")
    session = access_service.redeem_access_code(code, "agent-development")

    access_service.record_progress(session.session_id, "agent-development", 2, True)

    assert access_service.get_progress(session.session_id, "agent-development") == {2: True}
