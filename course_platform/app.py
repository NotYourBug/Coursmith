"""Production application factory: database work belongs to lifespan only."""
from contextlib import asynccontextmanager, closing
from pathlib import Path
import logging
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from .access import AccessService
from .audit import AuditEvent, record_denial
from .admin.auth import AdminService
from .admin.security import RateLimiter
from .database import LATEST_SCHEMA_VERSION, check_database, migrate_database, open_readonly, sync_course, transaction
from .content import load_course_package
from .content_inspection import inspect_package
from .delivery.entitlements import EntitlementService
from .delivery.progress import ProgressService
from .delivery.recovery import RecoveryService
from .domain import BusinessError, utc_now
from .operations.codes import CodeService
from .operations.legacy import LegacyService
from .operations.orders import OrderService
from .operations.products import ProductService
from .security import CsrfService
from .settings import Settings, load_settings

logger = logging.getLogger(__name__)
ROOT = Path(__file__).parent


def _initialize(app, settings):
    if settings.database_path.exists():
        if check_database(settings.database_path)["version"] != LATEST_SCHEMA_VERSION:
            raise BusinessError("offline_migration_required",
                "Startup requires an offline backup and explicit migration to latest5.", 409)
    else:
        migrate_database(settings.database_path)
    # A restored DB must explicitly relocate matched content before startup;
    # never use still-present paths from the original source configuration.
    with closing(open_readonly(settings.database_path)) as connection:
        for row in connection.execute("SELECT content_path FROM courses"):
            try:
                Path(row["content_path"]).resolve().relative_to(settings.content_root.resolve())
            except ValueError:
                raise BusinessError("content_relocation_required",
                    "Verified content relocation into the configured content root is required before startup.", 409) from None
    clock = utc_now
    app.state.settings = settings
    for name, service in (("admin_service", AdminService), ("csrf_service", CsrfService),
        ("rate_limiter", RateLimiter), ("product_service", ProductService), ("code_service", CodeService),
        ("order_service", OrderService), ("legacy_service", LegacyService)):
        setattr(app.state, name, service(settings.database_path, clock=clock))
    rights = EntitlementService(settings.database_path, clock=clock, session_ttl_hours=settings.session_ttl_hours)
    app.state.entitlement_service = rights
    app.state.recovery_service = RecoveryService(settings.database_path, clock=clock,
        session_ttl_hours=settings.session_ttl_hours, entitlement_service=rights)
    app.state.progress_service = ProgressService(settings.database_path, clock=clock, entitlement_service=rights)
    app.state.access_service = AccessService(settings.database_path, settings.session_ttl_hours, clock=clock)
    settings.content_root.mkdir(parents=True, exist_ok=True)
    for directory in sorted(settings.content_root.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        try:
            inspect_package(directory)
            package = load_course_package(directory)
        except (BusinessError, ValueError):
            logger.warning("content_scan_unavailable")
            continue
        if package.manifest.status != "published":
            continue
        sync_course(package.manifest, directory, settings.database_path)
        with transaction(settings.database_path, immediate=True) as connection:
            app.state.product_service.ensure_draft_in_tx(connection, package.manifest.course_id)


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        await run_in_threadpool(_initialize, app, settings or load_settings())
        yield

    app = FastAPI(title="智课工坊课程站", version="0.1.0", lifespan=lifespan)
    app.state.templates = Jinja2Templates(directory=str(ROOT / "templates"))
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        request.state.request_id = secrets.token_hex(16)
        try:
            response = await call_next(request)
        except Exception as error:
            # Catch before ServerErrorMiddleware rethrows to server logging.
            # Never log exception messages, payloads or traceback locals.
            response = await unexpected_error(request, error)
        response.headers.setdefault("X-Request-ID", request.state.request_id)
        if request.url.path.startswith(("/admin", "/access", "/learn", "/api/")):
            response.headers["Cache-Control"] = "no-store"
        if request.url.path.startswith("/admin"):
            response.headers.setdefault("Content-Security-Policy",
                "default-src 'none'; style-src 'self'; img-src 'self'; "
                f"script-src {app.state.settings.site_origin}/static/admin/; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        else:
            response.headers.setdefault("Content-Security-Policy",
                "default-src 'self'; script-src 'none'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; object-src 'none'; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'self'")
        for key, value in (("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"),
            # no-referrer makes native form POST Origin opaque ("null") in
            # Chromium. Preserve same-origin form Origin, without sending
            # referrers to another origin; strict Origin/CSRF checks still apply.
            ("Referrer-Policy", "same-origin"), ("Permissions-Policy", "camera=(), microphone=(), geolocation=()")):
            response.headers.setdefault(key, value)
        return response

    @app.exception_handler(BusinessError)
    async def business_error(request, error):
        if request.url.path.startswith(("/learn/", "/api/progress")) and not getattr(error, "denial_recorded", False):
            action = "progress.update" if request.method == "POST" else "learning.access"
            await run_in_threadpool(record_denial, app.state.settings.database_path,
                AuditEvent(None, "request", "delivery", action, error.code, "denied",
                    request.state.request_id, {"error_code": error.code}))
        response = HTMLResponse(f"<h1>请求不可用</h1><p>关联 ID：{request.state.request_id}</p>",
                                status_code=error.status_code, headers=getattr(error, "headers", {}))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Exception)
    async def unexpected_error(request, error):
        logger.error("request_failed type=%s request_id=%s", type(error).__name__, request.state.request_id)
        return HTMLResponse(f"<h1>服务暂不可用</h1><p>关联 ID：{request.state.request_id}</p>",
            status_code=500, headers={"Cache-Control": "no-store", "X-Request-ID": request.state.request_id,
                                    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"})

    from .admin.routes import auth, products, codes, orders, entitlements, legacy, dashboard
    from .routes import catalog, access, learning
    # Dashboard owns /admin in the composed factory; standalone auth router
    # still has its original home for earlier HTTP consumers.
    for router in (dashboard.router, auth.router, products.router, codes.router, orders.router,
                   entitlements.router, legacy.router, catalog.router, access.router, learning.router):
        app.include_router(router)
    return app
