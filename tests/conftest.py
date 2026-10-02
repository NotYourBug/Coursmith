import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI

from course_platform.settings import Settings


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
def admin_service(db_path, clock):
    from course_platform.admin.auth import AdminService

    return AdminService(db_path, clock=clock.now)


def make_http_app(routers: list[APIRouter], settings: Settings,
                  services: dict[str, object]) -> FastAPI:
    from fastapi.staticfiles import StaticFiles
    from fastapi.templating import Jinja2Templates

    app = FastAPI()
    root = Path(__file__).resolve().parent.parent / "course_platform"
    app.state.settings = settings
    app.state.templates = Jinja2Templates(directory=str(root / "templates"))
    for name, service in services.items():
        setattr(app.state, name, service)
    app.mount("/static", StaticFiles(directory=str(root / "static")), name="static")
    for router in routers:
        app.include_router(router)
    return app


@pytest.fixture
def http_app_factory():
    return make_http_app


@pytest.fixture
def admin_settings(db_path, tmp_path):
    from course_platform.settings import Settings

    return Settings(base_url="", api_key="", content_root=tmp_path / "content",
                    database_path=db_path, session_ttl_hours=72, environment="development",
                    site_origin="http://testserver")


@pytest.fixture
def admin_app(http_app_factory, admin_settings, admin_service, csrf_service, rate_limiter):
    from course_platform.admin.routes.auth import router

    return http_app_factory([router], admin_settings, {
        "admin_service": admin_service, "csrf_service": csrf_service,
        "rate_limiter": rate_limiter,
    })


@pytest.fixture
def admin_client(admin_app):
    from fastapi.testclient import TestClient

    with TestClient(admin_app, client=("198.51.100.7", 50000), follow_redirects=False) as client:
        yield client


@pytest.fixture
def buyer_client(admin_app):
    from fastapi.testclient import TestClient

    with TestClient(admin_app, client=("198.51.100.8", 50001), follow_redirects=False) as client:
        yield client


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
