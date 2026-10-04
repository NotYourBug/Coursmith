"""Validated course package exports."""

from __future__ import annotations

from html import escape
import mimetypes
import re
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

import tinycss2

from .content import load_course_package, validate_course_package
from .content_inspection import _package_root, _read_snapshot, _validate_snapshot, _confirm_snapshot
from .routes.learning import ChapterLayout, css_urls


class ExportError(RuntimeError):
    pass


_STYLE_RE = re.compile(r"(<style\b[^>]*>)(.*?)</style\s*>", re.I | re.S)
_PRINT_ORIGIN = "https://coursmith-print.invalid"


def _validated_package(course_dir: Path):
    report = validate_course_package(course_dir)
    if not report.ok:
        raise ExportError("course package is invalid: " + "; ".join(report.errors))
    return load_course_package(course_dir)


def _allowed_files(package):
    manifest = package.manifest
    relative_paths = {
        "manifest.json",
        "index.html",
        manifest.source_manifest,
        manifest.license_file,
    }
    relative_paths.update(chapter.path for chapter in manifest.chapters)
    for optional in ("CHANGELOG.md",):
        if (package.root / optional).is_file():
            relative_paths.add(optional)
    for directory in ("assets", "downloads"):
        root = package.root / directory
        if root.is_dir():
            relative_paths.update(
                file.relative_to(package.root).as_posix()
                for file in root.rglob("*")
                if file.is_file()
            )
    return sorted(relative_paths)


def export_course_zip(course_dir: Path, output_zip: Path) -> Path:
    package = _validated_package(Path(course_dir))
    output_zip = Path(output_zip)
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in _allowed_files(package):
            source = package.root / relative
            if source.is_file():
                archive.write(source, arcname=relative)
    return output_zip


def _print_snapshot(course_dir):
    try:
        root = _package_root(course_dir)
        files, inventory = _read_snapshot(root)
        manifest, dependencies = _validate_snapshot(files)
        _confirm_snapshot(root, files, inventory)
        return manifest, files, dependencies
    except (ValueError, OSError, RecursionError) as error:
        raise ExportError("Course package is unavailable for PDF export.") from error


def _chapter_css(text, number, base):
    """Scope document selectors to that chapter's preserved html/body wrappers."""
    rules = tinycss2.parse_stylesheet(css_urls(text, base), skip_comments=False, skip_whitespace=False)
    prefix = f'[data-print-chapter="{number}"] '
    def selector(tokens):
        result = []
        previous = None
        for token in tokens:
            if token.type == "ident" and token.lower_value == "root" and result and result[-1] == ":":
                result[-1] = "[data-print-html]"
            elif token.type == "ident" and token.lower_value in ("html", "body") and previous not in (".", ":"):
                result.append(f'[data-print-{token.lower_value}]')
            elif token.type == "function":
                result.append(token.name + "(" + selector(token.arguments) + ")")
            else:
                result.append(tinycss2.serialize([token]))
            if token.type not in ("whitespace", "comment"):
                previous = getattr(token, "value", None)
        return "".join(result)
    def scope(items):
        for rule in items:
            if rule.type == "qualified-rule":
                groups, pending = [], []
                for token in rule.prelude:
                    if token.type == "literal" and token.value == ",":
                        groups.append(prefix + selector(pending).strip())
                        pending = []
                    else:
                        pending.append(token)
                groups.append(prefix + selector(pending).strip())
                rule.prelude = tinycss2.parse_component_value_list(",".join(groups))
            elif rule.type == "at-rule" and rule.lower_at_keyword in ("media", "supports", "layer", "container") and rule.content is not None:
                nested = tinycss2.parse_rule_list(rule.content)
                scope(nested)
                rule.content = tinycss2.parse_component_value_list(tinycss2.serialize(nested))
    scope(rules)
    return tinycss2.serialize(rules)


def _compose_print_document(manifest, files) -> str:
    chapters: list[str] = []
    heads = []
    for chapter in manifest.chapters:
        base = f"{_PRINT_ORIGIN}/chapter-{chapter.number}/{chapter.path}"
        layout = ChapterLayout(base)
        layout.feed(files[chapter.path].decode("utf8"))
        layout.close()
        head = "".join(layout.head)
        def scoped_styles(fragment):
            return _STYLE_RE.sub(lambda match: match.group(1) + _chapter_css(match.group(2), chapter.number, base) + "</style>", fragment)
        heads.append(scoped_styles(head))
        chapters.append(
            f'<section class="print-chapter" data-print-chapter="{chapter.number}">'
            f'<div data-print-html {layout.html_attributes}><div data-print-body {layout.body_attributes}>'
            + scoped_styles("".join(layout.body)) + "</div></div></section>"
        )
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        f"<title>{escape(manifest.title)}</title>"
        "<style>@page{size:A4;margin:16mm}.print-chapter{break-before:page;}"
        ".print-chapter:first-of-type{break-before:auto}</style>"
        + "".join(heads)
        + "</head><body>"
        + "".join(chapters)
        + "</body></html>"
    )


def _build_print_document(course_dir: Path) -> str:
    manifest, files, _ = _print_snapshot(course_dir)
    return _compose_print_document(manifest, files)


def _print_with_playwright(
    course_dir: Path, output_pdf: Path, browser_executable: Path | None = None
) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on local install
        raise ExportError("Playwright 未安装，无法生成 PDF") from exc
    try:
        manifest, files, dependencies = _print_snapshot(Path(course_dir))
        document = _compose_print_document(manifest, files)
        resources = {}
        for chapter in manifest.chapters:
            reachable, pending = set(), [chapter.path]
            while pending:
                source = pending.pop()
                for relative in dependencies.get(source, ()):
                    if relative not in reachable:
                        reachable.add(relative)
                        pending.append(relative)
            for relative in reachable:
                url = f"{_PRINT_ORIGIN}/chapter-{chapter.number}/{relative}"
                payload = files[relative]
                if relative.lower().endswith(".css"):
                    payload = _chapter_css(payload.decode("utf8"), chapter.number, url).encode("utf8")
                resources[url] = (payload, mimetypes.guess_type(relative)[0] or "application/octet-stream")
        with sync_playwright() as playwright:
            launch_options = {"headless": True}
            if browser_executable:
                launch_options["executable_path"] = str(browser_executable)
            browser = playwright.chromium.launch(**launch_options)
            try:
                page = browser.new_page(java_script_enabled=False, service_workers="block")
                def resource(route):
                    parts = urlsplit(route.request.url)
                    url = parts._replace(path=unquote(parts.path), query="", fragment="").geturl()
                    if route.request.url == _PRINT_ORIGIN + "/course-print.html":
                        route.fulfill(body=document, content_type="text/html; charset=utf-8")
                    elif url in resources:
                        payload, content_type = resources[url]
                        route.fulfill(body=payload, content_type=content_type)
                    else:
                        route.abort()  # No filesystem or external-network fallback.
                page.route("**/*", resource)
                page.goto(_PRINT_ORIGIN + "/course-print.html", wait_until="networkidle")
                page.emulate_media(media="print")
                page.evaluate("document.fonts.ready")
                page.pdf(path=str(output_pdf), format="A4", print_background=True)
            finally:
                browser.close()
    except Exception as exc:
        raise ExportError(f"PDF 生成失败：{exc}") from exc


def export_course_pdf(
    course_dir: Path, output_pdf: Path, browser_executable: Path | None = None
) -> Path:
    _validated_package(Path(course_dir))
    output_pdf = Path(output_pdf)
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    _print_with_playwright(Path(course_dir), output_pdf, browser_executable)
    if not output_pdf.is_file() or output_pdf.stat().st_size == 0:
        raise ExportError("PDF 生成器未产生有效文件")
    return output_pdf
