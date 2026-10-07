import zipfile
import json
import os
import re
import struct
import zlib
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


@pytest.fixture
def inline_svg_pdf_package(two_directory_pdf_package):
    root = two_directory_pdf_package
    manifest = json.loads((root / "manifest.json").read_bytes())
    (root / "assets/icons.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><defs>'
        '<linearGradient id="paint"><stop stop-color="lime"/></linearGradient>'
        '</defs><rect id="shape" width="12" height="12" fill="url(#paint)"/></svg>', encoding="utf8")
    for chapter, color, clip_x, mask_y, base, use_attribute in zip(
            manifest["chapters"], ["red", "blue"], [0, 50], [0, 30],
            ["../../assets/", "../assets/"], ["href", "xlink:href"]):
        path = root / chapter["path"]
        original = path.read_text(encoding="utf8")
        style = ('<style>#heading {font-size:19px} :is(#heading) {line-height:31px}'
                 '[id="description"] {font-size:17px} [href="#heading"] {font-weight:600}'
                 '.inline-diagram[data-note="#paint"] {color:#0a141e; --literal:"#paint"}'
                 '</style>')
        diagram = (
            '<h2 id="heading">Local definitions</h2><p id="description">Literal #paint</p>'
            '<a class="local-link" href="#heading" aria-describedby="description">Local link</a>'
            f'<a class="self-link" href="{Path(chapter["path"]).name}#heading">Self link</a>'
            '<svg class="inline-diagram" data-note="#paint" xmlns="http://www.w3.org/2000/svg" '
            'xmlns:xlink="http://www.w3.org/1999/xlink" width="100" height="60" '
            'style="display:block;background:white"><defs>'
            f'<path id="shape" d="M{clip_x} 0H{clip_x + 50}V60H{clip_x}Z"/>'
            f'<linearGradient id="paint"><stop stop-color="{color}"/>'
            f'<stop offset="1" stop-color="{color}"/></linearGradient>'
            f'<clipPath id="clip"><rect x="{clip_x}" width="50" height="60"/></clipPath>'
            '<mask id="mask" maskUnits="userSpaceOnUse" x="0" y="0" width="100" height="60">'
            f'<rect y="{mask_y}" width="100" height="30" fill="white"/></mask></defs>'
            f'<use class="local-use" {use_attribute}="#shape" fill="url(#paint)" '
            'clip-path="url(&quot;#clip&quot;)" mask="url(#mask)"/></svg>'
            '<svg class="external-diagram" width="12" height="12" style="display:block;background:white">'
            f'<use class="external-use" href="{base}icons.svg#shape"/></svg>'
            f'<span class="external-paint" style="fill:url({base}icons.svg#paint)">External paint</span>'
        )
        path.write_text(original.replace("</head>", style + "</head>")
                        .replace("</body>", diagram + "</body>"), encoding="utf8")
        stylesheet = root / "assets" / ("theme.css" if color == "red" else "blue-theme.css")
        stylesheet.write_text(stylesheet.read_text(encoding="utf8")
                              + '.local-use {fill:url(#paint)}', encoding="utf8")
    return root


def test_pdf_keeps_inline_fragments_local_and_external_fragments_validated(inline_svg_pdf_package):
    from html.parser import HTMLParser
    from course_platform.content_inspection import inspect_package

    assert "assets/icons.svg" in inspect_package(inline_svg_pdf_package).preview_assets

    class Elements(HTMLParser):
        def __init__(self):
            super().__init__()
            self.chapter, self.elements = None, {"1": [], "2": []}

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if "data-print-chapter" in attrs:
                self.chapter = attrs["data-print-chapter"]
            if self.chapter:
                self.elements[self.chapter].append((tag, attrs))

    parsed = Elements()
    parsed.feed(_build_print_document(inline_svg_pdf_package))
    ids = []
    for number, elements in parsed.elements.items():
        chapter_ids = {attrs["id"] for _, attrs in elements if "id" in attrs}
        assert len(chapter_ids) == 6
        assert not chapter_ids & {"shape", "paint", "clip", "mask", "heading", "description"}
        ids.extend(chapter_ids)
        local = next(attrs for _, attrs in elements if attrs.get("class") == "local-use")
        for value in [local.get("href", local.get("xlink:href")), local["fill"], local["clip-path"], local["mask"]]:
            assert "https://coursmith-print.invalid/course-print.html#" in value
            assert re.search(r'#([^"\)]+)', value).group(1) in chapter_ids
        link = next(attrs for _, attrs in elements if attrs.get("class") == "local-link")
        assert link["href"].split("#")[1] in chapter_ids
        assert link["aria-describedby"] in chapter_ids
        assert next(attrs for _, attrs in elements if attrs.get("class") == "self-link")["href"] == link["href"]
        external = next(attrs for _, attrs in elements if attrs.get("class") == "external-use")
        assert external["href"] == f"https://coursmith-print.invalid/chapter-{number}/assets/icons.svg#shape"
        paint = next(attrs for _, attrs in elements if attrs.get("class") == "external-paint")
        assert f"/chapter-{number}/assets/icons.svg#paint" in paint["style"]
    assert len(set(ids)) == 12


def _screenshot_pixels(png, points):
    """Decode Chrome's 8-bit RGB(A) PNG, using only stdlib test utilities."""
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    offset, compressed = 8, bytearray()
    while offset < len(png):
        length = struct.unpack(">I", png[offset:offset + 4])[0]
        kind, data = png[offset + 4:offset + 8], png[offset + 8:offset + 8 + length]
        if kind == b"IHDR":
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
            assert depth == 8 and color in (2, 6) and (compression, filtering, interlace) == (0, 0, 0)
        elif kind == b"IDAT":
            compressed.extend(data)
        offset += length + 12
    channels = 3 if color == 2 else 4
    stride, raw = width * channels, zlib.decompress(compressed)
    rows, previous = [], bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        method, row = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        assert method in range(5)
        for x in range(stride):
            left = row[x - channels] if x >= channels else 0
            above, upper_left = previous[x], previous[x - channels] if x >= channels else 0
            predictor = left + above - upper_left
            nearest = min((left, above, upper_left), key=lambda value: abs(predictor - value))
            row[x] = (row[x] + (0, left, above, (left + above) // 2, nearest)[method]) % 256
        rows.append(row)
        previous = row
    return [tuple(rows[y][x * channels:x * channels + 3]) for x, y in points]


@pytest.mark.browser
def test_real_pdf_paints_chapter_local_svg_definitions(inline_svg_pdf_package, tmp_path, monkeypatch):
    import platform
    from playwright.sync_api import BrowserType, Page
    from course_platform.content_inspection import inspect_package

    fingerprint = inspect_package(inline_svg_pdf_package).fingerprint
    before = {p.relative_to(inline_svg_pdf_package): p.read_bytes()
              for p in inline_svg_pdf_package.rglob("*") if p.is_file()}
    real_launch, real_pdf = BrowserType.launch, Page.pdf

    def launch(browser_type, **options):
        if os.environ.get("COURSMITH_BROWSER_CHANNEL"):
            options["channel"] = os.environ["COURSMITH_BROWSER_CHANNEL"]
        return real_launch(browser_type, **options)

    def observed_print(page, **options):
        print(f"Supplementary actual renderer: Chrome {page.context.browser.version}; Python {platform.python_version()}")
        payload = real_pdf(page, **options)
        assert payload.startswith(b"%PDF-") and payload.rstrip().endswith(b"%%EOF")
        assert len(re.findall(rb"/Type /Page\b", payload)) == 2
        samples = [_screenshot_pixels(svg.screenshot(), [(25, 15), (75, 45), (75, 15), (25, 45)])
                   for svg in page.locator(".inline-diagram").all()]
        print(f"Actual print-page SVG pixels (use/gradient/clip/mask): {samples}")
        assert samples == [[(255, 0, 0), (255, 255, 255), (255, 255, 255), (255, 255, 255)],
                           [(255, 255, 255), (0, 0, 255), (255, 255, 255), (255, 255, 255)]]
        for svg in page.locator(".external-diagram").all():
            assert _screenshot_pixels(svg.screenshot(), [(6, 6)]) == [(0, 255, 0)]
        for chapter in page.locator(".print-chapter").all():
            assert chapter.locator("h2").evaluate("e => getComputedStyle(e).fontSize") == "19px"
            assert chapter.locator("h2").evaluate("e => getComputedStyle(e).lineHeight") == "31px"
            assert chapter.locator('p').last.evaluate("e => getComputedStyle(e).fontSize") == "17px"
            for link in chapter.locator(".local-link, .self-link").all():
                assert link.evaluate("e => getComputedStyle(e).fontWeight") == "600"
            assert chapter.locator(".inline-diagram").evaluate("e => getComputedStyle(e).color") == "rgb(10, 20, 30)"
            assert chapter.locator(".inline-diagram").evaluate("e => getComputedStyle(e).getPropertyValue('--literal').trim()") == '"#paint"'
        print("Validated external SVG fragments painted green; local ID/attribute selectors and quoted CSS literal retained; real two-page PDF produced")
        return payload

    monkeypatch.setattr(BrowserType, "launch", launch)
    monkeypatch.setattr(Page, "pdf", observed_print)
    output = export_course_pdf(inline_svg_pdf_package, tmp_path / "inline-definitions.pdf")
    assert output.read_bytes().startswith(b"%PDF-")
    assert inspect_package(inline_svg_pdf_package).fingerprint == fingerprint
    assert {p.relative_to(inline_svg_pdf_package): p.read_bytes()
            for p in inline_svg_pdf_package.rglob("*") if p.is_file()} == before


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
