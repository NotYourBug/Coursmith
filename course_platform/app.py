"""FastAPI application for the first-party course delivery site."""

from __future__ import annotations

from dataclasses import dataclass
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
            continue
        package = load_course_package(directory)
        if package.manifest.status != "published":
            continue
        catalog[package.manifest.slug] = CatalogItem(package)
        sync_course(package.manifest, directory, settings.database_path)
    return catalog


def _course_view(item: CatalogItem) -> dict[str, object]:
    manifest = item.manifest
    return {
        "slug": manifest.slug,
        "title": manifest.title,
        "category": manifest.category,
        "version": manifest.version,
        "chapter_count": manifest.chapter_count,
        "chapters": [chapter.model_dump() for chapter in manifest.chapters],
        "free_chapters": manifest.free_chapters,
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

    def render(request: Request, template: str, **context):
        return templates.TemplateResponse(
            request=request,
            name=template,
            context={"request": request, **context},
        )

    def get_item(slug: str) -> CatalogItem:
        item = app.state.catalog.get(slug)
        if item is None:
            raise HTTPException(status_code=404, detail="课程不存在")
        return item

    def require_access(request: Request, slug: str) -> tuple[CatalogItem, str]:
        item = get_item(slug)
        raw_session = request.cookies.get("course_session", "")
        try:
            session = app.state.access_service.require_session(
                raw_session, item.manifest.course_id
            )
        except InvalidSession as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return item, session.session_id

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
        return render(request, "access.html", error=None)

    @app.post("/access/redeem")
    async def redeem(request: Request):
        payload = parse_qs((await request.body()).decode("utf-8"))
        slug = payload.get("course_slug", [""])[0].strip()
        code = payload.get("code", [""])[0].strip()
        item = app.state.catalog.get(slug)
        if item is None or not code:
            return render(request, "access.html", error="请填写有效的课程和兑换码")
        try:
            session = app.state.access_service.redeem_access_code(
                code, item.manifest.course_id
            )
        except InvalidAccessCode as exc:
            return render(request, "access.html", error=str(exc))
        response = RedirectResponse(f"/learn/{slug}", status_code=303)
        response.set_cookie(
            "course_session",
            session.session_id,
            max_age=app.state.settings.session_ttl_hours * 3600,
            httponly=True,
            samesite="lax",
            secure=app.state.settings.environment == "production",
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

    @app.get("/learn/{course_slug}/chapters/{number}")
    def learn_chapter(request: Request, course_slug: str, number: int):
        item, _ = require_access(request, course_slug)
        chapter = next(
            (chapter for chapter in item.manifest.chapters if chapter.number == number),
            None,
        )
        if chapter is None:
            raise HTTPException(status_code=404, detail="章节不存在")
        chapter_path = (item.package.root / chapter.path).resolve()
        if not chapter_path.is_file():
            raise HTTPException(status_code=404, detail="章节文件不存在")
        return HTMLResponse(chapter_path.read_text(encoding="utf-8"))

    @app.get("/learn/{course_slug}/downloads/course.pdf")
    def download_pdf(request: Request, course_slug: str):
        item, _ = require_access(request, course_slug)
        pdf = item.package.root / "downloads" / "course.pdf"
        if not pdf.is_file():
            raise HTTPException(status_code=404, detail="PDF 尚未生成")
        return FileResponse(pdf, media_type="application/pdf", filename="course.pdf")

    @app.post("/api/progress", status_code=204)
    async def save_progress(request: Request):
        try:
            payload = await request.json()
            slug = str(payload.get("course_slug", ""))
            chapter_number = int(payload.get("chapter_number"))
            completed = bool(payload.get("completed"))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(status_code=400, detail="进度数据无效")
        if chapter_number <= 0:
            raise HTTPException(status_code=400, detail="章节编号必须为正整数")
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
                f"<p>{exc.detail}</p><a href='/'>返回课程站</a></body></html>",
                status_code=exc.status_code,
            )
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return app


app = create_app()
