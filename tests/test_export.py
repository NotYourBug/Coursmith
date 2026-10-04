import zipfile
import json
import os
import re
import pytest
from pathlib import Path

from course_platform.export import _build_print_document, export_course_pdf, export_course_zip


@pytest.fixture
def two_directory_pdf_package(fixture_package):
    # Both references are legal within the package, but differ by source base.
    root = fixture_package
    manifest = json.loads((root / "manifest.json").read_bytes())
    manifest["chapters"].append({**manifest["chapters"][0], "number": 2, "title": "Second unit", "free_preview": False})
    manifest["chapter_count"] = 2
    (root / "chapters").mkdir(exist_ok=True)
    (root / "lessons/unit").mkdir(parents=True)
    (root / "assets").mkdir(exist_ok=True)
    (root / "assets/red.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="30"><rect width="40" height="30" fill="red"/></svg>', encoding="utf8")
    (root / "assets/blue.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="60" height="20"><rect width="60" height="20" fill="blue"/></svg>', encoding="utf8")
    (root / "assets/theme.css").write_text('@import "nested.css"; body.unit { color: rgb(12, 34, 56); } .diagram {background-image: url("red.svg");}', encoding="utf8")
    (root / "assets/nested.css").write_text('.caption { font-size: 23px; } .caption[data-note=":root"] {font-weight:500}', encoding="utf8")
    (root / "assets/blue-theme.css").write_text('@import "blue-nested.css"; body.unit { color: rgb(65, 43, 21); } .diagram {background-image: url("blue.svg");}', encoding="utf8")
    (root / "assets/blue-nested.css").write_text('.caption { font-size: 27px; }', encoding="utf8")
    for chapter, path, base, image in zip(manifest["chapters"], ["lessons/unit/intro.html", "chapters/02.html"],
            ["../../assets/", "../assets/"], ["red.svg", "blue.svg"]):
        chapter["path"] = path
        stylesheet = "theme.css" if image == "red.svg" else "blue-theme.css"
        spacing = chapter["number"]
        (root / path).write_text(f'<!doctype html><html lang="en" dir="ltr"><head><title>Unit</title><link rel="stylesheet" href="{base}{stylesheet}"><style media="screen">.caption {{text-indent:13px}}</style></head><body class="unit" style="padding:7px"><style>.caption {{letter-spacing:{spacing}px}}</style><h1 class="caption" data-note=":root">PDF unit {image}</h1><img src="{base}{image}"><div class="diagram">Diagram</div></body></html>', encoding="utf8")
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf8")
    return root


def test_pdf_composition_keeps_each_validated_resource_base(two_directory_pdf_package):
    from html.parser import HTMLParser
    from urllib.parse import urljoin
    from course_platform.content_inspection import inspect_package
    assert inspect_package(two_directory_pdf_package).preview_assets >= {"assets/red.svg", "assets/theme.css", "assets/nested.css"}
    class Resources(HTMLParser):
        def __init__(self):
            super().__init__()
            self.base, self.images, self.styles = "", [], []
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "base":
                self.base = attrs["href"]
            elif tag == "img":
                self.images.append(urljoin(self.base, attrs["src"]))
            elif tag == "link" and attrs.get("rel") == "stylesheet":
                self.styles.append(urljoin(self.base, attrs["href"]))
    resources = Resources()
    resources.feed(_build_print_document(two_directory_pdf_package))
    # Resolve literal package targets independently of the composition algorithm.
    assert all(url.endswith("/assets/" + image) for url, image in zip(resources.images, ["red.svg", "blue.svg"]))
    assert len(resources.images) == 2 and len(resources.styles) == 2
    assert all(url.endswith("/assets/" + name) for url, name in zip(resources.styles, ["theme.css", "blue-theme.css"]))


@pytest.mark.browser
def test_real_pdf_renders_both_diagrams_and_linked_styles(two_directory_pdf_package, tmp_path, monkeypatch):
    from playwright.sync_api import BrowserType, Page
    real_launch, real_pdf = BrowserType.launch, Page.pdf
    def launch(browser_type, **options):
        channel = os.environ.get("COURSMITH_BROWSER_CHANNEL")
        if channel:
            options["channel"] = channel
        return real_launch(browser_type, **options)
    def print_observed_page(page, **options):
        # The actual exporter page must render the accepted diagrams/CSS before
        # the real Chrome printer produces the final bytes (no fake printer).
        assert page.locator("img").evaluate_all("xs => xs.map(x => [x.complete, x.naturalWidth, x.naturalHeight])") == [[True, 40, 30], [True, 60, 20]]
        for caption, size, color, spacing in zip(page.locator(".caption").all(), ["23px", "27px"], ["rgb(12, 34, 56)", "rgb(65, 43, 21)"], ["1px", "2px"]):
            assert caption.evaluate("e => getComputedStyle(e).fontSize") == size
            assert caption.evaluate("e => getComputedStyle(e).color") == color
            assert caption.evaluate("e => getComputedStyle(e).letterSpacing") == spacing
            assert caption.evaluate("e => getComputedStyle(e).textIndent") == "0px"
        assert page.locator(".caption").first.evaluate("e => getComputedStyle(e).fontWeight") == "500"
        for unit in page.locator(".unit").all():
            assert unit.evaluate("e => getComputedStyle(e).paddingTop") == "7px"
        assert page.locator('[data-print-html][lang="en"][dir="ltr"]').count() == 2
        print("Actual PDF renderer: " + page.context.browser.version + "; diagrams 40x30/60x20; distinct imported CSS 23px/27px; body colors/padding and html language/direction verified")
        return real_pdf(page, **options)
    monkeypatch.setattr(BrowserType, "launch", launch)
    monkeypatch.setattr(Page, "pdf", print_observed_page)
    output = export_course_pdf(two_directory_pdf_package, tmp_path / "real-course.pdf")
    pdf = output.read_bytes()
    assert pdf.startswith(b"%PDF-") and pdf.rstrip().endswith(b"%%EOF")
    assert len(re.findall(rb"/Type /Page\b", pdf)) == 2
    assert b"/MediaBox" in pdf and b"/Contents" in pdf and b"/Root" in pdf


@pytest.mark.parametrize("target", ["https://outside.example/image.svg", "../../../outside.svg"])
def test_pdf_composition_rejects_unchecked_resources(two_directory_pdf_package, target):
    from course_platform.export import ExportError
    path = two_directory_pdf_package / "lessons/unit/intro.html"
    path.write_text(path.read_text(encoding="utf8").replace("../../assets/red.svg", target), encoding="utf8")
    with pytest.raises(ExportError):
        _build_print_document(two_directory_pdf_package)


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
