"""All delivery aliases share current rights, verified bytes and format gates."""
import json
import mimetypes
import secrets
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import tinycss2
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from markupsafe import Markup
from starlette.concurrency import run_in_threadpool

from ..database import transaction
from ..delivery.entitlements import _hash
from ..domain import BusinessError
from ..security import check_origin, parse_unique_form, read_limited_body
from .access import cookie
from .catalog import content_bytes, course_row, learning_view, public_product, render, verified_manifest

router = APIRouter()


def authorize(request, slug, *, online=False):
    row = course_row(request, slug)
    raw = request.cookies.get(f"course_session_{slug}", "")
    try:
        session = request.app.state.entitlement_service.require_session(raw, row["course_id"])
    except BusinessError:
        # Distinguish broken current release bytes from a denied valid release.
        checked(row)
        raise
    if online and session.issued_policy and not session.issued_policy.access.online:
        raise BusinessError("online_unavailable", "Online learning is unavailable.", 403)
    return row, raw, session


def checked(row):
    try:
        return verified_manifest(row)
    except BusinessError:
        raise BusinessError("content_unavailable", "Course content is unavailable.", 503) from None


def csrf_for_get(request, row, raw, session):
    token = request.cookies.get(f"course_csrf_{row['slug']}", "")
    if not session.csrf_hash:
        token = secrets.token_urlsafe(32)
        with transaction(request.app.state.settings.database_path, immediate=True) as connection:
            # Re-authorize within the same writer lock before adding a nonce.
            current = request.app.state.entitlement_service.require_session_in_tx(connection, raw, row["course_id"])
            if not current.csrf_hash:
                connection.execute("UPDATE sessions SET csrf_hash=? WHERE session_hash=? AND csrf_hash IS NULL",
                    (_hash(token), current.session_hash))
            else:
                token = ""
    if token and session.csrf_hash:
        try:
            request.app.state.csrf_service.verify_bound_csrf(token, token, session.csrf_hash)
        except BusinessError:
            token = ""
    return token


def set_csrf_cookie(request, response, row, session, token):
    if token:
        seconds = max(1, int((session.session_expires_at - request.app.state.entitlement_service.clock()).total_seconds()))
        cookie(request, response, f"course_csrf_{row['slug']}", token, seconds=seconds)
    return response


def chapter_for(manifest, value):
    for chapter in manifest.chapters:
        if (type(value) is int and chapter.number == value) or (isinstance(value, str) and (
            chapter.path == value or Path(chapter.path).name == value)):
            return chapter
    raise BusinessError("chapter_missing", "Chapter is unavailable.", 404)


def css_urls(text, base):
    tokens = tinycss2.parse_component_value_list(text)
    def rewrite(items):
        for token in items:
            if token.type == "url":
                token.representation = "url(" + json.dumps(urljoin(base, token.value)) + ")"
            elif token.type == "function" and token.lower_name == "url":
                values = [t for t in token.arguments if t.type not in ("whitespace", "comment")]
                if len(values) == 1 and hasattr(values[0], "value"):
                    token.arguments = tinycss2.parse_component_value_list(json.dumps(urljoin(base, values[0].value)))
            elif hasattr(token, "content"):
                rewrite(token.content)
            elif hasattr(token, "arguments"):
                rewrite(token.arguments)
    rewrite(tokens)
    return tinycss2.serialize(tokens)


class ChapterLayout(HTMLParser):
    """Extract only a fully package-validated head/body; rewrite local URLs."""
    def __init__(self, base):
        super().__init__(convert_charrefs=False)
        self.base, self.part, self.style = base, None, False
        self.head, self.body = [], []
        self.body_attributes = ""

    def emit(self, text):
        if self.part:
            getattr(self, self.part).append(text)

    def handle_starttag(self, tag, attrs):
        if tag == "head":
            self.part = "head"
            return
        self.style = tag == "style" or self.style
        rendered = []
        for name, value in attrs:
            if value is None:
                rendered.append(name)
                continue
            if name == "style":
                value = css_urls(value, self.base)
            elif name in ("srcset", "imagesrcset"):
                value = ", ".join(" ".join([urljoin(self.base, bits[0]), *bits[1:]]) for bits in
                    (candidate.split() for candidate in value.split(",")))
            elif name in ("src", "href", "xlink:href", "poster", "background", "cite", "longdesc", "lowsrc", "dynsrc"):
                value = urljoin(self.base, value)
            elif "url(" in value.lower() or "\\" in value:
                value = css_urls(value, self.base)
            rendered.append(f'{name}="{escape(value, quote=True)}"')
        if tag == "body":
            self.part = "body"
            self.body_attributes = " ".join(rendered)
        else:
            self.emit("<" + tag + (" " + " ".join(rendered) if rendered else "") + ">")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in ("head", "body"):
            self.part = None
        else:
            self.emit("</" + tag + ">")
        if tag == "style":
            self.style = False

    def handle_data(self, data):
        self.emit(css_urls(data, self.base) if self.style else data)

    def handle_entityref(self, name):
        self.emit("&" + name + ";")

    def handle_charref(self, name):
        self.emit("&#" + name + ";")


def chapter_page(request, slug, value, *, preview=False):
    if preview:
        public_product(request, slug)
        row = course_row(request, slug)
        raw = session = None
    else:
        row, raw, session = authorize(request, slug, online=True)
    manifest, _ = checked(row)
    chapter = chapter_for(manifest, value)
    if preview and not chapter.free_preview:
        raise BusinessError("preview_denied", "Chapter is unavailable.", 403)
    prefix = f"/courses/{slug}/" if preview else f"/learn/{slug}/"
    layout = ChapterLayout(prefix + chapter.path)
    layout.feed(content_bytes(row, chapter.path).decode("utf8"))
    token = csrf_for_get(request, row, raw, session) if session else None
    progress = request.app.state.progress_service.get_progress(raw, row["course_id"]) if session else {}
    index = manifest.chapters.index(chapter)
    response = render(request, "learn_chapter.html", course=learning_view(manifest), chapter=chapter,
        chapter_head=Markup("".join(layout.head)), chapter_body=Markup("".join(layout.body)),
        body_attributes=Markup(layout.body_attributes),
        completed=progress.get(chapter.number, False), csrf_token=token, preview=preview,
        previous=manifest.chapters[index-1] if index else None,
        next=manifest.chapters[index+1] if index+1 < len(manifest.chapters) else None,
        prefix=prefix.rstrip('/'))
    return set_csrf_cookie(request, response, row, session, token) if session else response


@router.get("/learn/{slug}")
def hub(request: Request, slug: str):
    row, raw, session = authorize(request, slug)
    manifest, _ = checked(row)
    course = learning_view(manifest)
    policy = session.issued_policy.access if session.issued_policy else None
    online = policy is None or policy.online
    token = csrf_for_get(request, row, raw, session)
    progress = request.app.state.progress_service.get_progress(raw, row["course_id"]) if online else {}
    for chapter in course["chapters"]:
        chapter["completed"] = progress.get(chapter["number"], False)
    response = render(request, "learn_home.html", course=course, online=online,
        pdf=policy.pdf if policy else False, zip=policy.zip if policy else False, csrf_token=token,
        expiry=session.entitlement_expires_at, policy_known=policy is not None)
    return set_csrf_cookie(request, response, row, session, token)


@router.get("/learn/{slug}/chapters/{number:int}")
def chapter_number(request: Request, slug: str, number: int):
    return chapter_page(request, slug, number)


@router.get("/learn/{slug}/chapters/index.html")
@router.get("/learn/{slug}/index.html")
def index_alias(request: Request, slug: str):
    authorize(request, slug, online=True)
    return RedirectResponse(f"/learn/{slug}", status_code=303)


@router.get("/learn/{slug}/chapters/{filename}")
def chapter_filename(request: Request, slug: str, filename: str):
    return chapter_page(request, slug, filename)


def asset_response(request, slug, path, *, preview=False):
    if preview:
        public_product(request, slug)
        row = course_row(request, slug)
    else:
        row, _, _ = authorize(request, slug, online=True)
    _, inspection = checked(row)
    relative = "assets/" + path
    if relative not in (inspection.preview_assets if preview else inspection.all_assets):
        raise BusinessError("asset_missing", "Asset is unavailable.", 404)
    return Response(content_bytes(row, relative),
        media_type=mimetypes.guess_type(relative)[0] or "application/octet-stream")


@router.get("/learn/{slug}/assets/{path:path}")
def paid_asset(request: Request, slug: str, path: str):
    return asset_response(request, slug, path)


@router.get("/courses/{slug}/assets/{path:path}")
def preview_asset(request: Request, slug: str, path: str):
    return asset_response(request, slug, path, preview=True)


@router.get("/courses/{slug}/chapters/{number:int}")
def preview_number(request: Request, slug: str, number: int):
    return chapter_page(request, slug, number, preview=True)


@router.get("/courses/{slug}/chapters/index.html")
@router.get("/courses/{slug}/index.html")
def preview_index(request: Request, slug: str):
    public_product(request, slug)
    return RedirectResponse(f"/courses/{slug}", status_code=303)


@router.get("/courses/{slug}/chapters/{filename}")
def preview_filename(request: Request, slug: str, filename: str):
    return chapter_page(request, slug, filename, preview=True)


@router.get("/learn/{slug}/downloads/course.{format}")
def download(request: Request, slug: str, format: str):
    row, _, session = authorize(request, slug)
    if not session.issued_policy or format not in ("pdf", "zip") or not getattr(session.issued_policy.access, format):
        raise BusinessError("download_denied", "Download is unavailable.", 403)
    _, inspection = checked(row)
    if not getattr(inspection, format + "_ready"):
        raise BusinessError("download_missing", "Download is unavailable.", 404)
    return Response(content_bytes(row, f"downloads/course.{format}"),
        media_type="application/pdf" if format == "pdf" else "application/zip",
        headers={"Content-Disposition": f'attachment; filename="course.{format}"'})


def save(request, slug, number, completed, token):
    row, raw, session = authorize(request, slug, online=True)
    request.app.state.csrf_service.verify_bound_csrf(token, request.cookies.get(f"course_csrf_{slug}", ""), session.csrf_hash)
    manifest, _ = checked(row)
    chapter_for(manifest, number)
    request.app.state.progress_service.set_completed(raw, row["course_id"], number, completed)


@router.post("/learn/{slug}/chapters/{number:int}/progress")
async def save_form(request: Request, slug: str, number: int):
    check_origin(request, request.app.state.settings.site_origin)
    body = await read_limited_body(request, 65536)
    if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
        raise BusinessError("invalid_form", "Form required.", 415)
    form = parse_unique_form(body)
    if form.get("completed") not in ("true", "false"):
        raise BusinessError("invalid_progress", "Boolean completion required.", 400)
    await run_in_threadpool(save, request, slug, number, form["completed"] == "true", form.get("csrf_token", ""))
    return RedirectResponse(f"/learn/{slug}/chapters/{number}", status_code=303)


@router.post("/api/progress", status_code=204)
async def save_api(request: Request):
    check_origin(request, request.app.state.settings.site_origin)
    body = await read_limited_body(request, 16384)
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise BusinessError("invalid_progress", "JSON required.", 415)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    try:
        payload = json.loads(body, object_pairs_hook=unique)
        if (not isinstance(payload, dict) or not isinstance(payload.get("course_slug"), str)
            or type(payload.get("chapter_number")) is not int or payload["chapter_number"] < 1
            or type(payload.get("completed")) is not bool):
            raise ValueError
    except (ValueError, TypeError, UnicodeError):
        raise BusinessError("invalid_progress", "Invalid progress.", 400) from None
    await run_in_threadpool(save, request, payload["course_slug"], payload["chapter_number"], payload["completed"], payload.get("csrf_token", ""))
    return Response(status_code=204)


# Nonstandard validated chapter paths retain their exact relative-resource base.
@router.get("/learn/{slug}/{path:path}")
def learning_path(request: Request, slug: str, path: str):
    return chapter_page(request, slug, path)


@router.get("/courses/{slug}/{path:path}")
def preview_path(request: Request, slug: str, path: str):
    return chapter_page(request, slug, path, preview=True)
