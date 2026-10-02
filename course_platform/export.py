"""Validated course package exports."""

from __future__ import annotations

from html import escape
import re
import tempfile
import zipfile
from pathlib import Path

from .content import load_course_package, validate_course_package


class ExportError(RuntimeError):
    pass


_BODY_RE = re.compile(r"<body\b[^>]*>(.*?)</body\s*>", re.I | re.S)
_STYLE_RE = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", re.I | re.S)


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


def _build_print_document(course_dir: Path) -> str:
    package = load_course_package(course_dir)
    styles: list[str] = []
    chapters: list[str] = []
    for chapter, chapter_file in zip(package.manifest.chapters, package.chapter_files):
        text = chapter_file.read_text(encoding="utf-8")
        styles.extend(_STYLE_RE.findall(text))
        body_match = _BODY_RE.search(text)
        if body_match is None:
            raise ExportError(f"章节缺少 body：{chapter.path}")
        chapters.append(
            f'<section class="print-chapter">{body_match.group(1)}</section>'
        )
    chapter_parents = {path.parent for path in package.chapter_files}
    resource_root = chapter_parents.pop() if len(chapter_parents) == 1 else package.root
    base_href = resource_root.resolve().as_uri().rstrip("/") + "/"
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        f"<base href='{escape(base_href, quote=True)}'>"
        f"<title>{escape(package.manifest.title)}</title>"
        "<style>@page{size:A4;margin:16mm}.print-chapter{break-before:page;}"
        ".print-chapter:first-of-type{break-before:auto}</style>"
        + "".join(f"<style>{style}</style>" for style in styles)
        + "</head><body>"
        + "".join(chapters)
        + "</body></html>"
    )


def _print_with_playwright(
    course_dir: Path, output_pdf: Path, browser_executable: Path | None = None
) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on local install
        raise ExportError("Playwright 未安装，无法生成 PDF") from exc
    try:
        with tempfile.TemporaryDirectory(prefix="coursmith-pdf-") as temp_dir:
            print_html = Path(temp_dir) / "course-print.html"
            print_html.write_text(_build_print_document(Path(course_dir)), encoding="utf-8")
            with sync_playwright() as playwright:
                launch_options = {
                    "headless": True,
                    "args": ["--allow-file-access-from-files"],
                }
                if browser_executable:
                    launch_options["executable_path"] = str(browser_executable)
                browser = playwright.chromium.launch(**launch_options)
                page = browser.new_page()
                page.goto(print_html.resolve().as_uri(), wait_until="networkidle")
                page.emulate_media(media="print")
                page.evaluate("document.fonts.ready")
                page.pdf(path=str(output_pdf), format="A4", print_background=True)
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
