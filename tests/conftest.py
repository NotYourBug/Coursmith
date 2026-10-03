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


@pytest.fixture
def product_service(db_path, clock):
    from course_platform.operations.products import ProductService

    return ProductService(db_path, clock=clock.now)


@pytest.fixture
def product_owner(admin_service, actor):
    admin_service.initialize_owner("owner", "example-pass-123")
    return actor


@pytest.fixture
def published_product_data(product_service, product_owner, fixture_package, db_path):
    from course_platform.content import CourseManifest
    from course_platform.content_inspection import inspect_package
    from course_platform.database import sync_course, transaction
    from course_platform.operations.products import AccessPolicy, ProductInput, SalesChannel

    manifest = CourseManifest.model_validate_json((fixture_package / "manifest.json").read_bytes())
    sync_course(manifest, fixture_package, db_path)
    inspection = inspect_package(fixture_package)
    with transaction(db_path) as connection:
        connection.execute("UPDATE courses SET package_hash=? WHERE course_id=?",
                           (inspection.fingerprint, manifest.course_id))
    category_id = product_service.save_category(product_owner, None, None, "technical", "技术", 0, True)
    return ProductInput(title="可售课程", category_id=category_id, synopsis="课程简介",
        audience="初学者", prerequisites="无", outcomes=["完成练习"], course_id=manifest.course_id,
        ai_disclosure=manifest.ai_disclosure, support_text="邮件支持",
        channels=[SalesChannel(name="店铺", url="https://shop.example/course")],
        policy=AccessPolicy(access_mode="days", access_days=30, online=True, pdf=False,
                            zip=False, update_policy="current_version"))


@pytest.fixture
def active_product(product_service, product_owner, published_product_data):
    from course_platform.operations.products import SalesChecklist

    draft = product_service.create(product_owner, published_product_data)
    return product_service.activate(product_owner, draft.id, draft.revision,
        SalesChecklist(quality=True, sources=True, ai=True, mobile=True, downloads=True))


@pytest.fixture
def product_app(http_app_factory, admin_settings, admin_service, csrf_service, rate_limiter,
                product_service):
    from course_platform.admin.routes.auth import router as auth_router
    from course_platform.admin.routes.products import router

    return http_app_factory([auth_router, router], admin_settings, {
        "admin_service": admin_service, "csrf_service": csrf_service,
        "rate_limiter": rate_limiter, "product_service": product_service,
    })


@pytest.fixture
def product_client(product_app):
    from fastapi.testclient import TestClient

    with TestClient(product_app, client=("198.51.100.7", 50000), follow_redirects=False) as client:
        yield client


@pytest.fixture
def code_service(db_path, clock):
    from course_platform.operations.codes import CodeService

    return CodeService(db_path, clock=clock.now)


@pytest.fixture
def code_app(http_app_factory, admin_settings, admin_service, csrf_service, rate_limiter,
             product_service, code_service):
    from course_platform.admin.routes.auth import router as auth_router
    from course_platform.admin.routes.products import router as product_router
    from course_platform.admin.routes.codes import router

    return http_app_factory([auth_router, product_router, router], admin_settings, {
        "admin_service": admin_service, "csrf_service": csrf_service,
        "rate_limiter": rate_limiter, "product_service": product_service, "code_service": code_service,
    })


@pytest.fixture
def code_client(code_app):
    from fastapi.testclient import TestClient

    with TestClient(code_app, client=("198.51.100.7", 50000), follow_redirects=False) as client:
        yield client
