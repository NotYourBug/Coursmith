"""Validated course package exports."""

from __future__ import annotations

from html import escape
import json
import mimetypes
import re
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit

import tinycss2

from .content import load_course_package, validate_course_package
from .content_inspection import _package_root, _read_snapshot, _validate_snapshot, _confirm_snapshot
from .routes.learning import ChapterLayout, css_urls


class ExportError(RuntimeError):
    pass


_STYLE_RE = re.compile(r"(<style\b[^>]*>)(.*?)</style\s*>", re.I | re.S)
_PRINT_ORIGIN = "https://coursmith-print.invalid"
_PRINT_DOCUMENT = _PRINT_ORIGIN + "/course-print.html"
_ID_REFERENCES = {
    "for", "headers", "list", "form", "itemref", "aria-activedescendant",
    "aria-labelledby", "aria-describedby", "aria-controls", "aria-owns",
    "aria-flowto", "aria-details", "aria-errormessage",
}
_URL_ATTRIBUTES = {
    "src", "href", "xlink:href", "poster", "background", "cite", "longdesc", "lowsrc", "dynsrc",
}
_CSS_ATTRIBUTES = {
    "style", "fill", "stroke", "filter", "clip-path", "mask", "marker",
    "marker-start", "marker-mid", "marker-end", "cursor",
}


def _print_id(number, value):
    return f"coursmith-print-{number}-{value}"


def _print_reference(value, number, base):
    target = urljoin(base, value.strip())
    parts = urlsplit(target)
    original = urlsplit(value.strip())
    if parts.fragment and not parts.query and (
        (not original.scheme and not original.netloc and not original.path)
        or parts._replace(fragment="").geturl() == base
    ):
        return _PRINT_DOCUMENT + "#" + quote(_print_id(number, unquote(parts.fragment)), safe="")
    return target  # A validated external asset retains its own fragment owner.


def _print_css_urls(text, number, base):
    """Normalize URL tokens only, leaving colors and quoted literals untouched."""
    tokens = tinycss2.parse_component_value_list(text)

    def rewrite(items):
        for token in items:
            if token.type == "url":
                token.representation = "url(" + json.dumps(_print_reference(token.value, number, base), ensure_ascii=False) + ")"
            elif token.type == "function" and token.lower_name == "url":
                values = [t for t in token.arguments if t.type not in ("whitespace", "comment")]
                if len(values) == 1 and hasattr(values[0], "value"):
                    token.arguments = tinycss2.parse_component_value_list(
                        json.dumps(_print_reference(values[0].value, number, base), ensure_ascii=False))
            elif hasattr(token, "content"):
                rewrite(token.content)
            elif hasattr(token, "arguments"):
                rewrite(token.arguments)

    rewrite(tokens)
    return css_urls(tinycss2.serialize(tokens), base)


def _print_attribute(name, value, number, base):
    if name == "id":
        return _print_id(number, value)
    if name in _ID_REFERENCES:
        return " ".join(_print_id(number, item) for item in value.split())
    if name in _URL_ATTRIBUTES:
        return _print_reference(value, number, base)
    if name in ("srcset", "imagesrcset"):
        return ", ".join(" ".join([_print_reference(bits[0], number, base), *bits[1:]])
                         for bits in (candidate.split() for candidate in value.split(",")))
    if name in _CSS_ATTRIBUTES or "url(" in value.lower() or "\\" in value:
        return _print_css_urls(value, number, base)
    return value


class _PrintChapterLayout(ChapterLayout):
    """Print-only IDs/references, before the shared extractor rebases URLs."""

    def __init__(self, base, number):
        super().__init__(base)
        self.number = number

    def handle_starttag(self, tag, attrs, *, self_closing=False):
        normalized = [(name, _print_attribute(name, value, self.number, self.base)
                       if value is not None else None) for name, value in attrs]
        super().handle_starttag(tag, normalized, self_closing=self_closing)

    def handle_data(self, data):
        if self.style:
            data = _print_css_urls(data, self.number, self.base)
        super().handle_data(data)


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
    rules = tinycss2.parse_stylesheet(_print_css_urls(text, number, base), skip_comments=False, skip_whitespace=False)
    prefix = f'[data-print-chapter="{number}"] '
    def selector(tokens):
        result = []
        previous = None
        for token in tokens:
            if token.type == "hash" and token.is_identifier:
                token.value = _print_id(number, token.value)
                result.append(tinycss2.serialize([token]))
            elif token.type == "[] block":
                values = [t for t in token.content if t.type not in ("whitespace", "comment")]
                if (len(values) >= 3 and values[0].type == "ident"
                        and values[1].type == "literal" and values[1].value in ("=", "~=")
                        and values[2].type in ("ident", "string")):
                    attribute, value = values[0].lower_value, values[2]
                    if attribute == "id" or attribute in _ID_REFERENCES | _URL_ATTRIBUTES:
                        value.value = _print_attribute(attribute, value.value, number, base)
                        if value.type == "string":
                            value.representation = json.dumps(value.value, ensure_ascii=False)
                result.append(tinycss2.serialize([token]))
            elif token.type == "ident" and token.lower_value == "root" and result and result[-1] == ":":
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
        layout = _PrintChapterLayout(base, chapter.number)
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
