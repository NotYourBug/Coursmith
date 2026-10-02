import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture
def fixture_package(tmp_path):
    source = Path(__file__).parent / "fixtures" / "course-package"
    target = tmp_path / "fixture-course"
    shutil.copytree(source, target)
    return target


class FrozenClock:
    def __init__(self):
        self._now = datetime(2026, 10, 2, tzinfo=timezone.utc)

    def now(self):
        return self._now

    def advance(self, **timedelta_kwargs):
        self._now += timedelta(**timedelta_kwargs)


@pytest.fixture
def clock():
    return FrozenClock()


@pytest.fixture
def actor():
    from course_platform.domain import Actor

    return Actor(admin_id=1, request_id="test-request")


@pytest.fixture
def db_path(tmp_path):
    from course_platform.database import migrate_database

    path = tmp_path / "operations.db"
    migrate_database(path)
    return path


@pytest.fixture
def csrf_service(db_path, clock):
    from course_platform.security import CsrfService

    return CsrfService(db_path, clock=clock.now)


@pytest.fixture
def rate_limiter(db_path, clock):
    from course_platform.admin.security import RateLimiter

    return RateLimiter(db_path, clock=clock.now)


@pytest.fixture
def legacy_db(tmp_path):
    from course_platform.database import SCHEMA

    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT INTO courses VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("course-1", "course-one", "课程", "AI", "1.0", "published", "content", "2026-10-02T00:00:00+00:00"),
        )
        connection.execute("INSERT INTO chapters VALUES ('course-1', 1, '第一章', '01.html', 0)")
        connection.execute(
            "INSERT INTO access_codes VALUES (1, 'course-1', ?, '2026-10-02', NULL, '2026-10-02')",
            ("a" * 64,),
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?, 'course-1', '2026-10-02', '2026-10-05')",
            ("b" * 64,),
        )
        connection.execute(
            "INSERT INTO progress VALUES (?, 'course-1', 1, 1, '2026-10-02')", ("b" * 64,)
        )
        connection.execute("INSERT INTO events VALUES (1, 'redeemed', 'course-1', ?, '{}', '2026-10-02')", ("b" * 64,))
        connection.commit()
    finally:
        connection.close()
    return path
