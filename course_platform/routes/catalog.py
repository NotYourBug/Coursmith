"""Public product copy and verified course projections."""
from contextlib import closing
import json
from pathlib import Path

from fastapi import APIRouter, Request

from ..content import CourseManifest
from ..content_inspection import inspect_package, read_verified_file
from ..database import open_readonly
from ..domain import BusinessError

router = APIRouter()


def render(request, template, *, status=200, **context):
    return request.app.state.templates.TemplateResponse(request=request, name=template,
        context={"request_id": request.state.request_id, **context}, status_code=status)


def content_bytes(row, relative):
    try:
        return read_verified_file(Path(row["content_path"]), relative, row["package_hash"])
    except BusinessError:
        raise BusinessError("content_unavailable", "Course content is unavailable.", 503) from None


def course_row(request, slug):
    with closing(open_readonly(request.app.state.settings.database_path)) as connection:
        row = connection.execute("SELECT course_id, slug, version, content_path, package_hash FROM courses WHERE slug=?", (slug,)).fetchone()
    if not row:
        raise BusinessError("course_missing", "Course is unavailable.", 404)
    return dict(row)


def verified_manifest(row):
    root = Path(row["content_path"])
    try:
        inspection = inspect_package(root)
    except BusinessError:
        raise BusinessError("content_unavailable", "Course content is unavailable.", 503) from None
    if not row["package_hash"] or inspection.fingerprint != row["package_hash"]:
        raise BusinessError("content_unavailable", "Course content is unavailable.", 503)
    manifest = CourseManifest.model_validate_json(content_bytes(row, "manifest.json"))
    if (manifest.course_id, manifest.slug, manifest.version) != (row["course_id"], row["slug"], row["version"]):
        raise BusinessError("content_unavailable", "Course content is unavailable.", 503)
    return manifest, inspection


def products(request):
    with closing(open_readonly(request.app.state.settings.database_path)) as connection:
        records = connection.execute("""SELECT c.slug, c.version, p.title, p.status, p.description,
            k.name AS category, k.slug AS category_slug
            FROM products p JOIN courses c ON c.course_id=p.course_id
            JOIN categories k ON k.id=p.category_id WHERE p.status IN ('active','paused') AND k.enabled=1
            ORDER BY p.id""").fetchall()
    result = []
    for record in records:
        data = json.loads(record["description"])
        result.append({**{key: record[key] for key in ("slug", "version", "title", "status", "category", "category_slug")}, **{key: data.get(key) for key in (
            "synopsis", "audience", "prerequisites", "outcomes", "ai_disclosure", "support_text", "channels", "policy")}})
    return result


def public_product(request, slug):
    item = next((p for p in products(request) if p["slug"] == slug), None)
    if not item:
        raise BusinessError("product_missing", "Course is unavailable.", 404)
    return item


def learning_view(manifest):
    return {"slug": manifest.slug, "title": manifest.title, "version": manifest.version,
        "category": manifest.category, "chapters": [c.model_dump() for c in manifest.chapters],
        "free_chapters": manifest.free_chapters, "preview_chapter": next(iter(manifest.free_chapters), None),
        "chapter_count": manifest.chapter_count, "ai_disclosure": manifest.ai_disclosure}


@router.get("/")
def home(request: Request):
    items = products(request)
    return render(request, "home.html", courses=items,
        categories={p["category_slug"]: p["category"] for p in items})


@router.get("/categories/{slug}")
def category(request: Request, slug: str):
    items = [p for p in products(request) if p["category_slug"] == slug]
    if not items:
        raise BusinessError("category_missing", "Category is unavailable.", 404)
    return render(request, "category.html", category=items[0]["category"], courses=items)


@router.get("/courses/{slug}")
def detail(request: Request, slug: str):
    item = public_product(request, slug)
    manifest, _ = verified_manifest(course_row(request, slug))
    return render(request, "course_detail.html", course={**learning_view(manifest), **item})


@router.get("/help")
def help_page(request: Request):
    return render(request, "help.html")
