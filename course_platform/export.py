"""Validated course package exports."""

from __future__ import annotations

import zipfile
from pathlib import Path

from .content import load_course_package, validate_course_package


class ExportError(RuntimeError):
    pass


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


def _print_with_playwright(
    course_dir: Path, output_pdf: Path, browser_executable: Path | None = None
) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on local install
        raise ExportError("Playwright 未安装，无法生成 PDF") from exc
    try:
        with sync_playwright() as playwright:
            launch_options = {"headless": True}
            if browser_executable:
                launch_options["executable_path"] = str(browser_executable)
            browser = playwright.chromium.launch(**launch_options)
            page = browser.new_page()
            page.goto((Path(course_dir) / "index.html").resolve().as_uri(), wait_until="networkidle")
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
