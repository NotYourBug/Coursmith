"""FastAPI application for the first-party course delivery site."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
import json
import logging
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .access import AccessService, InvalidAccessCode, InvalidSession
from .content import CoursePackage, load_course_package, validate_course_package
from .database import initialize_database, sync_course
from .settings import Settings, load_settings


TEMPLATES_DIR = Path(__file__).parent / "templates"
MAX_FORM_BODY_BYTES = 8 * 1024
MAX_JSON_BODY_BYTES = 16 * 1024
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CatalogItem:
    package: CoursePackage

    @property
    def manifest(self):
        return self.package.manifest


def _load_catalog(settings: Settings) -> dict[str, CatalogItem]:
    catalog: dict[str, CatalogItem] = {}
    settings.content_root.mkdir(parents=True, exist_ok=True)
    for directory in sorted(settings.content_root.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        report = validate_course_package(directory)
        if not report.ok:
            logger.warning(
                "Skipping invalid course package %s: %s",
                directory,
                "; ".join(report.errors),
            )
            continue
        package = load_course_package(directory)
        if package.manifest.status != "published":
            continue
        catalog[package.manifest.slug] = CatalogItem(package)
        sync_course(package.manifest, directory, settings.database_path)
    return catalog


def _course_view(item: CatalogItem) -> dict[str, object]:
    manifest = item.manifest
    preview = next((chapter.number for chapter in manifest.chapters if chapter.free_preview), None)
    return {
        "slug": manifest.slug,
        "title": manifest.title,
        "category": manifest.category,
        "version": manifest.version,
        "chapter_count": manifest.chapter_count,
        "chapters": [chapter.model_dump() for chapter in manifest.chapters],
        "free_chapters": manifest.free_chapters,
        "preview_chapter": preview,
        "ai_disclosure": manifest.ai_disclosure,
        "outcome": "掌握一套可复用、可实践、可持续更新的课程知识体系。",
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or load_settings()
    initialize_database(runtime_settings.database_path)
    catalog = _load_catalog(runtime_settings)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app = FastAPI(title="智课工坊课程站", version="0.1.0")
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "static")),
        name="static",
    )
    app.state.settings = runtime_settings
    app.state.catalog = catalog
    app.state.access_service = AccessService(
        runtime_settings.database_path,
        session_ttl_hours=runtime_settings.session_ttl_hours,
    )
    app.state.templates = templates

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'none'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.url.path.startswith("/learn/"):
            response.headers["Cache-Control"] = "private, no-store"
        return response

    def render(request: Request, template: str, status_code: int = 200, **context):
        return templates.TemplateResponse(
            request=request,
            name=template,
            context={"request": request, **context},
            status_code=status_code,
        )

    def get_item(slug: str) -> CatalogItem:
        item = app.state.catalog.get(slug)
        if item is None:
            raise HTTPException(status_code=404, detail="课程不存在")
        return item

    def require_access(request: Request, slug: str) -> tuple[CatalogItem, str]:
        item = get_item(slug)
        raw_session = request.cookies.get(f"course_session_{slug}", "")
        try:
            session = app.state.access_service.require_session(
                raw_session, item.manifest.course_id
            )
        except InvalidSession as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return item, session.session_id

    def get_chapter(item: CatalogItem, number: int):
        chapter = next(
            (entry for entry in item.manifest.chapters if entry.number == number),
            None,
        )
        if chapter is None:
            raise HTTPException(status_code=404, detail="章节不存在")
        return chapter

    def get_chapter_by_filename(item: CatalogItem, filename: str):
        chapter = next(
            (
                entry
                for entry in item.manifest.chapters
                if Path(entry.path).name == filename
            ),
            None,
        )
        if chapter is None:
            raise HTTPException(status_code=404, detail="章节不存在")
        return chapter

    def serve_chapter(item: CatalogItem, chapter) -> HTMLResponse:
        chapter_path = (item.package.root / chapter.path).resolve()
        if not chapter_path.is_file():
            raise HTTPException(status_code=404, detail="章节文件不存在")
        return HTMLResponse(chapter_path.read_text(encoding="utf-8"))

    def serve_asset(item: CatalogItem, asset_path: str) -> FileResponse:
        assets_root = (item.package.root / "assets").resolve()
        candidate = (assets_root / asset_path).resolve()
        if (
            not assets_root.is_dir()
            or assets_root not in candidate.parents
            or not candidate.is_file()
            or candidate.is_symlink()
        ):
            raise HTTPException(status_code=404, detail="课程资源不存在")
        return FileResponse(candidate)

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        items = [_course_view(item) for item in app.state.catalog.values()]
        categories = sorted({item["category"] for item in items})
        return render(request, "home.html", courses=items, categories=categories)

    @app.get("/categories/{slug}", response_class=HTMLResponse)
    def category(request: Request, slug: str):
        items = [
            _course_view(item)
            for item in app.state.catalog.values()
            if item.manifest.category == slug
        ]
        if not items:
            raise HTTPException(status_code=404, detail="分类不存在或暂无课程")
        return render(request, "category.html", category=slug, courses=items)

    @app.get("/courses/{slug}", response_class=HTMLResponse)
    def course_detail(request: Request, slug: str):
        return render(request, "course_detail.html", course=_course_view(get_item(slug)))

    @app.get("/access", response_class=HTMLResponse)
    def access_page(request: Request):
        return render(
            request,
            "access.html",
            error=None,
            courses=[_course_view(item) for item in app.state.catalog.values()],
            selected_course=request.query_params.get("course", ""),
        )

    @app.post("/access/redeem")
    async def redeem(request: Request):
        body = await request.body()
        if len(body) > MAX_FORM_BODY_BYTES:
            raise HTTPException(status_code=413, detail="请求内容过大")
        try:
            payload = parse_qs(body.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="兑换请求编码无效") from exc
        slug = payload.get("course_slug", [""])[0].strip()
        code = payload.get("code", [""])[0].strip()
        item = app.state.catalog.get(slug)
        if item is None or not code:
            return render(
                request,
                "access.html",
                status_code=400,
                error="请填写有效的课程和兑换码",
                courses=[_course_view(entry) for entry in app.state.catalog.values()],
                selected_course=slug,
            )
        try:
            session = app.state.access_service.redeem_access_code(
                code, item.manifest.course_id
            )
        except InvalidAccessCode as exc:
            return render(
                request,
                "access.html",
                status_code=400,
                error=str(exc),
                courses=[_course_view(entry) for entry in app.state.catalog.values()],
                selected_course=slug,
            )
        response = RedirectResponse(f"/learn/{slug}", status_code=303)
        response.set_cookie(
            f"course_session_{slug}",
            session.session_id,
            max_age=app.state.settings.session_ttl_hours * 3600,
            httponly=True,
            samesite="lax",
            secure=app.state.settings.environment == "production",
            path="/",
        )
        return response

    @app.get("/help", response_class=HTMLResponse)
    def help_page(request: Request):
        return render(request, "help.html")

    @app.get("/learn/{course_slug}", response_class=HTMLResponse)
    def learn_home(request: Request, course_slug: str):
        item, session_id = require_access(request, course_slug)
        progress = app.state.access_service.get_progress(
            session_id, item.manifest.course_id
        )
        course = _course_view(item)
        for chapter in course["chapters"]:
            chapter["completed"] = progress.get(chapter["number"], False)
        return render(request, "learn_home.html", course=course)

    @app.get("/learn/{course_slug}/chapters/{number:int}")
    def learn_chapter(request: Request, course_slug: str, number: int):
        item, _ = require_access(request, course_slug)
        return serve_chapter(item, get_chapter(item, number))

    @app.get("/learn/{course_slug}/chapters/index.html")
    def learn_index_alias(request: Request, course_slug: str):
        require_access(request, course_slug)
        return RedirectResponse(f"/learn/{course_slug}", status_code=303)

    @app.get("/learn/{course_slug}/chapters/{filename}")
    def learn_chapter_file(request: Request, course_slug: str, filename: str):
        item, _ = require_access(request, course_slug)
        return serve_chapter(item, get_chapter_by_filename(item, filename))

    @app.get("/learn/{course_slug}/assets/{asset_path:path}")
    def learn_asset(request: Request, course_slug: str, asset_path: str):
        item, _ = require_access(request, course_slug)
        return serve_asset(item, asset_path)

    @app.get("/courses/{course_slug}/chapters/{number:int}")
    def preview_chapter(course_slug: str, number: int):
        item = get_item(course_slug)
        chapter = get_chapter(item, number)
        if not chapter.free_preview:
            raise HTTPException(status_code=403, detail="该章节不属于免费试学内容")
        return serve_chapter(item, chapter)

    @app.get("/courses/{course_slug}/chapters/index.html")
    def preview_index_alias(course_slug: str):
        get_item(course_slug)
        return RedirectResponse(f"/courses/{course_slug}", status_code=303)

    @app.get("/courses/{course_slug}/chapters/{filename}")
    def preview_chapter_file(course_slug: str, filename: str):
        item = get_item(course_slug)
        chapter = get_chapter_by_filename(item, filename)
        if not chapter.free_preview:
            raise HTTPException(status_code=403, detail="该章节不属于免费试学内容")
        return serve_chapter(item, chapter)

    @app.get("/courses/{course_slug}/assets/{asset_path:path}")
    def preview_asset(course_slug: str, asset_path: str):
        return serve_asset(get_item(course_slug), asset_path)

    @app.get("/learn/{course_slug}/downloads/course.pdf")
    def download_pdf(request: Request, course_slug: str):
        item, _ = require_access(request, course_slug)
        pdf = item.package.root / "downloads" / "course.pdf"
        if not pdf.is_file():
            raise HTTPException(status_code=404, detail="PDF 尚未生成")
        return FileResponse(pdf, media_type="application/pdf", filename="course.pdf")

    @app.post("/api/progress", status_code=204)
    async def save_progress(request: Request):
        body = await request.body()
        if len(body) > MAX_JSON_BODY_BYTES:
            raise HTTPException(status_code=413, detail="请求内容过大")
        try:
            payload = json.loads(body)
            slug = str(payload.get("course_slug", ""))
            chapter_number = payload.get("chapter_number")
            completed = payload.get("completed")
        except (ValueError, TypeError, AttributeError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="进度数据无效")
        if (
            not isinstance(chapter_number, int)
            or isinstance(chapter_number, bool)
            or chapter_number <= 0
            or not isinstance(completed, bool)
        ):
            raise HTTPException(status_code=400, detail="章节编号或完成状态无效")
        item, session_id = require_access(request, slug)
        if chapter_number not in {chapter.number for chapter in item.manifest.chapters}:
            raise HTTPException(status_code=404, detail="章节不存在")
        try:
            app.state.access_service.record_progress(
                session_id, item.manifest.course_id, chapter_number, completed
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return None

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if exc.status_code in (403, 404):
            return HTMLResponse(
                f"<!doctype html><html lang='zh-CN'><body><h1>{exc.status_code}</h1>"
                f"<p>{escape(str(exc.detail))}</p><a href='/'>返回课程站</a></body></html>",
                status_code=exc.status_code,
            )
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return app


app = create_app()
