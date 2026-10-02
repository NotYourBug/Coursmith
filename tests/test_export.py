import zipfile
from pathlib import Path

from course_platform.export import _build_print_document, export_course_pdf, export_course_zip


def test_zip_export_contains_manifest_sources_and_license(fixture_package, tmp_path):
    output = export_course_zip(fixture_package, tmp_path / "fixture.zip")

    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())

    assert "manifest.json" in names
    assert "SOURCES.txt" in names
    assert "LICENSE.txt" in names


def test_pdf_export_requires_a_valid_course_package(fixture_package, tmp_path, monkeypatch):
    def fake_print(_course_dir, output_pdf, _browser_executable=None):
        Path(output_pdf).write_bytes(b"%PDF-1.7 test")

    monkeypatch.setattr("course_platform.export._print_with_playwright", fake_print)

    output = export_course_pdf(fixture_package, tmp_path / "fixture.pdf")

    assert output == tmp_path / "fixture.pdf"
    assert output.read_bytes().startswith(b"%PDF")


def test_pdf_print_document_contains_chapter_content(fixture_package):
    document = _build_print_document(fixture_package)

    assert "第一章" in document
    assert "课程内容" in document
