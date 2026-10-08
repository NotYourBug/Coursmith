import shutil
import sqlite3
import zipfile
import json
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
def entitlement_service(db_path, clock):
    from course_platform.delivery.entitlements import EntitlementService

    return EntitlementService(db_path, clock=clock.now)


@pytest.fixture
def order_service(db_path, clock):
    from course_platform.operations.orders import OrderService

    return OrderService(db_path, clock=clock.now)


@pytest.fixture
def progress_service(db_path, clock, entitlement_service):
    from course_platform.delivery.progress import ProgressService

    return ProgressService(db_path, clock=clock.now, entitlement_service=entitlement_service)


@pytest.fixture
def issued_code(code_service, active_product, actor):
    from course_platform.operations.codes import BatchInput

    return code_service.issue_batch(actor, BatchInput(product_id=active_product.id, purpose="sale"),
                                   "issued-code").codes[0]


@pytest.fixture
def redeemed(entitlement_service, issued_code):
    return entitlement_service.redeem(issued_code.raw_code, expected_course_id=None, request_id="redeem-1")


@pytest.fixture
def recovery_service(db_path, entitlement_service):
    from course_platform.delivery.recovery import RecoveryService

    return RecoveryService(db_path, clock=entitlement_service.clock,
        session_ttl_hours=entitlement_service.session_ttl_hours, entitlement_service=entitlement_service)


@pytest.fixture
def verified_gift(code_service, active_product, actor, entitlement_service, db_path, clock):
    from course_platform.database import to_db_time, transaction
    from course_platform.operations.codes import BatchInput

    code = code_service.issue_batch(actor, BatchInput(product_id=active_product.id, purpose="gift"), "gift").codes[0]
    receipt = entitlement_service.redeem(code.raw_code, expected_course_id=None, request_id="gift-redeem")
    with transaction(db_path) as connection:
        connection.execute("UPDATE entitlements SET verified_at=?, verified_by=?, verified_reason=? WHERE id=?",
            (to_db_time(clock.now()), actor.admin_id, "核对发行用途 gift 并人工确认受赠者", receipt.session.entitlement_id))
    return receipt


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


@pytest.fixture
def delivery_content(fixture_package):
    manifest = json.loads((fixture_package / "manifest.json").read_text(encoding="utf8"))
    manifest["chapter_count"] = 2
    manifest["chapters"].append(dict(number=2, title="第二章", path="chapters/02.html", free_preview=False))
    (fixture_package / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf8")
    (fixture_package / "assets").mkdir(exist_ok=True)
    (fixture_package / "assets" / "free.png").write_bytes(b"free image")
    (fixture_package / "assets" / "paid.png").write_bytes(b"paid image")
    (fixture_package / "assets" / "theme.css").write_text('body { background: url("free.png"); }', encoding="utf8")
    (fixture_package / "chapters" / "01.html").write_text('<!doctype html><html><head><title>第一章</title><link rel="stylesheet" href="../assets/theme.css"></head><body class="learning-content" style="background-image:url(../assets/free.png)"><h1>第一章</h1><img src="../assets/free.png"></body></html>', encoding="utf8")
    (fixture_package / "chapters" / "02.html").write_text('<!doctype html><html><head><title>第二章</title></head><body><h1>第二章</h1><img src="../assets/paid.png"></body></html>', encoding="utf8")
    (fixture_package / "downloads").mkdir(exist_ok=True)
    (fixture_package / "downloads" / "course.pdf").write_bytes(b"%PDF-1.4\nfixture")
    with zipfile.ZipFile(fixture_package / "downloads" / "course.zip", "w") as archive:
        for path in fixture_package.rglob("*"):
            if path.is_file() and path.name != "course.zip":
                archive.write(path, path.relative_to(fixture_package).as_posix())
    return fixture_package


@pytest.fixture
def delivery_client(db_path, delivery_content, active_product, clock, monkeypatch):
    from fastapi.testclient import TestClient
    from course_platform.app import create_app
    fixture_package = delivery_content
    monkeypatch.setattr("course_platform.app.utc_now", clock.now)
    app = create_app(Settings("", "", fixture_package.parent, db_path, 72, "test", "http://testserver"))
    with TestClient(app, client=("198.51.100.8", 50001), follow_redirects=False) as client:
        yield client


@pytest.fixture
def release_content(delivery_content):
    """Accepted nested resources and SVG, prepared before actual sale readiness."""
    import base64

    package = delivery_content
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf8"))
    manifest["chapters"][0]["path"] = "lessons/unit/intro.html"
    # Nonascending declared order is an accepted immutable release contract.
    manifest["chapters"].reverse()
    (package / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf8")
    nested = package / "lessons" / "unit"
    nested.mkdir(parents=True)
    (nested / "intro.html").write_text('''<!doctype html><html><head><title>第一章</title>
<style>@import /* accepted */ "../../assets/theme.css";
.image-set {background-image:image-set("../../assets/free.png" 1x);width:20px;height:20px}</style>
</head><body><h1>第一章</h1><div class="image-set"></div><img src="../../assets/free.png">
<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20" viewBox="0 0 40 20">
<path id="left" d="M0 0 H20 V20 H0 Z" fill="red"/><path id="right" d="M20 0 H40 V20 H20 Z" fill="blue"/>
</svg><p>Matched restored lesson bytes</p></body></html>''', encoding="utf8")
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
    (package / "assets" / "free.png").write_bytes(png)
    (package / "assets" / "theme.css").write_text('h1 {color:rgb(12, 34, 56)}', encoding="utf8")
    with zipfile.ZipFile(package / "downloads" / "course.zip", "w") as archive:
        for path in package.rglob("*"):
            if path.is_file() and path.name != "course.zip":
                archive.write(path, path.relative_to(package).as_posix())
    return package


@pytest.fixture
def server_factory(tmp_path):
    """Run the actual CLI or documented factory contract over a real socket."""
    import os
    import socket
    import subprocess
    import sys
    import time
    from contextlib import contextmanager
    from types import SimpleNamespace

    import httpx

    @contextmanager
    def start(content, database, *, python=None, cwd=None, production=False,
              trusted="", cli=True, tls=False):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"{'https' if tls else 'http'}://127.0.0.1:{port}"
        origin = "https://courses.example" if production else url
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.update(COURSE_DATABASE=str(database.resolve()), COURSE_CONTENT_ROOT=str(content.resolve()),
                   COURSE_SITE_ORIGIN=origin, COURSE_ENVIRONMENT="production" if production else "test",
                   COURSE_SESSION_TTL_HOURS="72", COURSE_TRUSTED_PROXY_CIDRS=trusted,
                   DEEPSEEK_API_KEY="", PYTHONUTF8="1")
        args = [str(python or sys.executable), "-X", "utf8", "-m"]
        args += (["course_platform.cli", "serve", "--host", "127.0.0.1", "--port", str(port)] if cli else
                 ["uvicorn", "course_platform.app:create_app", "--factory", "--no-proxy-headers",
                  "--host", "127.0.0.1", "--port", str(port)])
        if tls:
            from datetime import datetime, timedelta, timezone
            from ipaddress import ip_address
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.x509.oid import NameOID
            assert not cli, "TLS uses the documented direct factory server contract"
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
            now = datetime.now(timezone.utc)
            cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                    .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
                    .not_valid_after(now+timedelta(days=1)).add_extension(x509.SubjectAlternativeName([
                        x509.IPAddress(ip_address("127.0.0.1"))]), critical=False).sign(key, hashes.SHA256()))
            cert_path, key_path = tmp_path / f"test-{port}.pem", tmp_path / f"test-{port}.key"
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                   serialization.NoEncryption()))
            args += ["--ssl-certfile", str(cert_path), "--ssl-keyfile", str(key_path)]
        log_path = tmp_path / f"server-{port}.log"
        with log_path.open("wb") as log:
            process = subprocess.Popen(args, cwd=cwd or Path(__file__).resolve().parents[1], env=env,
                                       stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        pytest.fail("Real server startup failed; see temporary server log")
                    try:
                        if httpx.get(url + "/help", timeout=1, verify=not tls, trust_env=False).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(.05)
                else:
                    pytest.fail("Real server readiness timeout (environment failure, never skip)")
                yield SimpleNamespace(url=url, origin=origin, db=database, content=content,
                                      log=log_path, process=process)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
    return start


@pytest.fixture
def live_site(release_content, tmp_path, server_factory):
    from course_platform.admin.auth import AdminService
    from course_platform.database import migrate_database

    path = tmp_path / "browser.db"
    migrate_database(path)
    # The only initial service operation; daily business operations are UI-only.
    AdminService(path).initialize_owner("owner", "example-pass-123")
    with server_factory(release_content.parent, path, cli=False, tls=True) as site:
        yield site


@pytest.fixture
def real_browser():
    import os
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        channel = os.environ.get("COURSMITH_BROWSER_CHANNEL", "").strip()
        browser = playwright.chromium.launch(**({"channel": channel} if channel else {}))
        print(f"Actual browser: {browser.version}; channel={channel or 'bundled chromium'}")
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def mobile_page(real_browser):
    context = real_browser.new_context(viewport={"width": 390, "height": 844}, ignore_https_errors=True)
    try:
        yield context.new_page()
    finally:
        context.close()
