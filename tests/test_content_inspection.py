import json
import io
import os
import subprocess
import stat
import zipfile
import zlib
from pathlib import Path

import pytest

from course_platform.content_inspection import inspect_package, read_verified_file
from course_platform.domain import BusinessError


def html(body, head=""):
    return f"<!doctype html><html><head>{head}</head><body>{body}</body></html>"


@pytest.fixture
def package_with_free_and_paid_assets(fixture_package):
    root = fixture_package
    data = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    data["chapter_count"] = 2
    data["chapters"].append(
        {
            "number": 2,
            "title": "Paid",
            "path": "chapters/02.html",
            "free_preview": False,
        }
    )
    (root / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    (root / "assets").mkdir()
    (root / "downloads").mkdir()
    (root / "chapters/01.html").write_text(
        html(
            '<img srcset="../assets/free.png 1x, ../assets/free%402.png 2x">'
            '<a href="../assets/paid.png">attachment</a>',
            "<link rel=stylesheet href=../assets/shared.css>",
        ),
        encoding="utf-8",
    )
    (root / "chapters/02.html").write_text(
        html('<img src="../assets/paid.png">'), encoding="utf-8"
    )
    (root / "assets/shared.css").write_text(
        '@import "nested.css"; @import url("nested.css");', encoding="utf-8"
    )
    (root / "assets/nested.css").write_text(
        '.free {background: url("free.png")} @font-face {src: url(font.woff2)}',
        encoding="utf-8",
    )
    for name in ("free.png", "free@2.png", "paid.png", "unused.png", "font.woff2"):
        (root / "assets" / name).write_bytes(name.encode())
    (root / "downloads/course.pdf").write_bytes(b"%PDF-1.7\nfixture\n%%EOF")
    return root


def test_preview_assets_include_only_transitive_free_dependencies(
    package_with_free_and_paid_assets,
):
    result = inspect_package(package_with_free_and_paid_assets)
    assert result.preview_assets == frozenset(
        {
            "assets/shared.css",
            "assets/nested.css",
            "assets/free.png",
            "assets/free@2.png",
            "assets/font.woff2",
        }
    )
    assert {"assets/paid.png", "assets/unused.png"} <= result.all_assets
    assert (result.course_id, result.slug, result.version) == (
        "fixture-course",
        "fixture-course",
        "0.1.0",
    )
    assert len(result.fingerprint) == 64


def test_paid_only_asset_is_not_public(package_with_free_and_paid_assets):
    result = inspect_package(package_with_free_and_paid_assets)
    assert {"assets/shared.css", "assets/free.png"} <= result.preview_assets
    assert "assets/paid.png" not in result.preview_assets
    assert "assets/paid.png" in result.all_assets


@pytest.mark.parametrize(
    "relative",
    ["chapters/01.html", "downloads/course.pdf", "assets/unused.png", "LICENSE.txt"],
)
def test_package_fingerprint_changes_on_body_or_download_edit(
    package_with_free_and_paid_assets, relative
):
    root = package_with_free_and_paid_assets
    before = inspect_package(root).fingerprint
    path = root / relative
    path.write_bytes(path.read_bytes() + b"\nchanged")
    assert inspect_package(root).fingerprint != before


def test_fingerprint_ignores_enumeration_order_and_directory_timestamps(
    package_with_free_and_paid_assets, monkeypatch
):
    root = package_with_free_and_paid_assets
    before = inspect_package(root).fingerprint
    original = os.scandir

    class ReversedScan:
        def __init__(self, path):
            with original(path) as entries:
                self.entries = iter(reversed(list(entries)))

        def __enter__(self):
            return self.entries

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(os, "scandir", ReversedScan)
    os.utime(root, (1000000000, 1000000000))
    assert inspect_package(root).fingerprint == before


@pytest.mark.parametrize(
    "reference",
    [
        "../../outside.png",
        "%2e%2e/%2e%2e/outside.png",
        "%252e%252e/outside.png",
        "..\\assets\\free.png",
        "..%5cassets/free.png",
        "/assets/free.png",
        "//example.com/free.png",
        "https://example.com/free.png",
        "data:image/png;base64,AA==",
        "file:///etc/passwd",
        "../downloads/course.pdf",
        "../chapters/02.html",
    ],
)
def test_asset_path_escape_is_rejected(package_with_free_and_paid_assets, reference):
    root = package_with_free_and_paid_assets
    (root / "chapters/01.html").write_text(
        html(f'<img src="{reference}">'), encoding="utf-8"
    )
    with pytest.raises(BusinessError):
        inspect_package(root)


def test_css_cycles_and_duplicate_dependencies_terminate(
    package_with_free_and_paid_assets,
):
    root = package_with_free_and_paid_assets
    (root / "assets/nested.css").write_text(
        '@import "shared.css"; a {background:url(free.png)}', encoding="utf-8"
    )
    assert inspect_package(root).preview_assets == frozenset(
        {
            "assets/shared.css",
            "assets/nested.css",
            "assets/free.png",
            "assets/free@2.png",
        }
    )


@pytest.mark.parametrize(
    "css",
    [
        '@import "https://example.com/a.css";',
        r'body {background: u\72l("https://example.com/x.png")}',
        'body {background: url("../../escape.png")}',
        '@import url("%2e%2e/%2e%2e/escape.css");',
        'body {background: url("..\\\\escape.png")}',
        'body {background: url("missing.png")}',
    ],
)
def test_unsafe_transitive_css_is_rejected(package_with_free_and_paid_assets, css):
    (package_with_free_and_paid_assets / "assets/nested.css").write_text(
        css, encoding="utf-8"
    )
    with pytest.raises(BusinessError):
        inspect_package(package_with_free_and_paid_assets)


@pytest.mark.parametrize(
    "body",
    [
        "<img src=../assets/free.png onerror=steal()>",
        "<a href=javascript:steal()>go</a>",
        '<a href="java&#x09;script:steal()">go</a>',
        "<iframe src=../chapters/02.html></iframe>",
        "<svg><a xlink:href=javascript:steal()>go</a></svg>",
    ],
)
def test_unsafe_changed_body_is_not_served(package_with_free_and_paid_assets, body):
    root = package_with_free_and_paid_assets
    fingerprint = inspect_package(root).fingerprint
    (root / "chapters/01.html").write_text(html(body), encoding="utf-8")
    for expected in (fingerprint, None):
        with pytest.raises(BusinessError):
            read_verified_file(root, "chapters/01.html", expected)


def test_verified_read_returns_bytes_and_rejects_stale_fingerprint(
    package_with_free_and_paid_assets,
):
    root = package_with_free_and_paid_assets
    fingerprint = inspect_package(root).fingerprint
    assert read_verified_file(root, "assets/free%402.png", fingerprint) == b"free@2.png"
    assert read_verified_file(root, "assets/free.png", None) == b"free.png"
    assert read_verified_file(root, "assets/free.png", "") == b"free.png"
    (root / "assets/free.png").write_bytes(b"replacement")
    with pytest.raises(BusinessError):
        read_verified_file(root, "assets/free.png", fingerprint)


@pytest.mark.parametrize(
    "relative",
    [
        "../manifest.json",
        "%2e%2e/secret",
        "assets\\free.png",
        "/manifest.json",
        "C:/secret",
        "assets/missing.png",
    ],
)
def test_verified_read_rejects_unsafe_or_missing_paths(
    package_with_free_and_paid_assets, relative
):
    with pytest.raises(BusinessError):
        read_verified_file(package_with_free_and_paid_assets, relative, None)


@pytest.mark.parametrize("kind", ["file", "directory", "root", "normalized"])
def test_symlink_in_any_path_segment_is_rejected(
    package_with_free_and_paid_assets, tmp_path, kind
):
    root = package_with_free_and_paid_assets
    # Real in-root links are forbidden too, even when they cannot escape.
    if kind == "file":
        target = root / "assets/link.png"
        destination = root / "assets/free.png"
    elif kind == "directory":
        target = root / "linked"
        destination = root / "assets"
    else:
        target = tmp_path / "root-link"
        destination = root
        root = target if kind == "root" else target / ".." / destination.name
    try:
        target.symlink_to(destination, target_is_directory=kind != "file")
    except OSError as exc:
        if getattr(exc, "winerror", None) != 1314:
            raise
        if kind == "file":
            pytest.skip("Windows denies file symlink creation (WinError 1314)")
        # Junctions exercise real directory reparse points without elevation.
        link_literal = str(target).replace("'", "''")
        destination_literal = str(destination).replace("'", "''")
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"New-Item -ItemType Junction -Path '{link_literal}' -Target '{destination_literal}' -ErrorAction Stop | Out-Null",
            ],
            check=True,
            capture_output=True,
        )
    with pytest.raises(BusinessError):
        inspect_package(root)
    with pytest.raises(BusinessError):
        read_verified_file(root, "assets/free.png", None)


def test_read_rejects_file_change_during_snapshot(
    package_with_free_and_paid_assets, monkeypatch
):
    root = package_with_free_and_paid_assets
    original = Path.open
    changed = False

    def changing_open(path, *args, **kwargs):
        nonlocal changed
        stream = original(path, *args, **kwargs)
        if path == root / "assets/free.png" and not changed:
            changed = True
            with original(path, "wb") as writer:
                writer.write(b"changed during read")
        return stream

    monkeypatch.setattr(Path, "open", changing_open)
    with pytest.raises(BusinessError):
        read_verified_file(root, "assets/free.png", None)


@pytest.mark.parametrize(
    "pdf, ready",
    [
        (b"", False),
        (b"not PDF", False),
        (b"%PDF-", False),
        (b"%PDF-1.7\nfixture", True),
    ],
)
def test_pdf_readiness_checks_header(package_with_free_and_paid_assets, pdf, ready):
    root = package_with_free_and_paid_assets
    (root / "downloads/course.pdf").write_bytes(pdf)
    assert inspect_package(root).pdf_ready is ready


@pytest.mark.parametrize(
    "member",
    [None, "../escape", "/escape", r"dir\escape", "%2e%2e/escape", "C:/escape"],
)
def test_zip_readiness_requires_manifest_members_and_safe_paths(
    package_with_free_and_paid_assets, member
):
    root = package_with_free_and_paid_assets
    with zipfile.ZipFile(root / "downloads/course.zip", "w") as archive:
        for relative in (
            "manifest.json",
            "index.html",
            "SOURCES.txt",
            "LICENSE.txt",
            "chapters/01.html",
            "chapters/02.html",
        ):
            archive.writestr(relative, (root / relative).read_bytes())
        if member:
            # ZipInfo normally rewrites Windows backslashes; retain malicious bytes.
            info = zipfile.ZipInfo()
            info.filename = member
            archive.writestr(info, b"unsafe")
    result = inspect_package(root)
    assert result.zip_ready is (member is None)
    (root / "downloads/course.zip").write_bytes(b"broken ZIP")
    assert inspect_package(root).zip_ready is False


def test_missing_downloads_are_not_ready_and_incomplete_zip_is_not_ready(
    fixture_package,
):
    assert inspect_package(fixture_package).pdf_ready is False
    assert inspect_package(fixture_package).zip_ready is False
    (fixture_package / "downloads").mkdir()
    with zipfile.ZipFile(fixture_package / "downloads/course.zip", "w") as archive:
        archive.writestr("manifest.json", b"{}")
    assert inspect_package(fixture_package).zip_ready is False


def test_read_detects_same_size_rewrite_with_restored_mtime(
    package_with_free_and_paid_assets, monkeypatch
):
    import tinycss2

    root = package_with_free_and_paid_assets
    path = root / "assets/free.png"
    before = path.stat()
    original = tinycss2.parse_component_value_list
    changed = False

    def change_during_validation(*args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            path.write_bytes(b"evil.png")  # same length; Windows ctime is creation time
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        return original(*args, **kwargs)

    monkeypatch.setattr(
        tinycss2, "parse_component_value_list", change_during_validation
    )
    with pytest.raises(BusinessError):
        read_verified_file(root, "assets/free.png", None)


def test_stylesheet_import_closure_does_not_depend_on_filename_extension(
    package_with_free_and_paid_assets,
):
    root = package_with_free_and_paid_assets
    (root / "chapters/01.html").write_text(
        html("free", "<link rel=stylesheet href=../assets/style>"), encoding="utf-8"
    )
    (root / "assets/style").write_text('@import "nested.data";', encoding="utf-8")
    (root / "assets/nested.data").write_text(
        "body {background:url(free.png)}", encoding="utf-8"
    )
    assert inspect_package(root).preview_assets == frozenset(
        {"assets/style", "assets/nested.data", "assets/free.png"}
    )
    (root / "assets/nested.data").write_text(
        "body {background:url(https://example.com/p.png)}", encoding="utf-8"
    )
    with pytest.raises(BusinessError):
        inspect_package(root)


def test_non_embedded_links_are_not_preview_assets(package_with_free_and_paid_assets):
    root = package_with_free_and_paid_assets
    (root / "chapters/01.html").write_text(
        html("free", "<link rel=canonical href=../assets/paid.png>"), encoding="utf-8"
    )
    assert inspect_package(root).preview_assets == frozenset()


def test_css_image_set_string_dependencies_are_included(
    package_with_free_and_paid_assets,
):
    root = package_with_free_and_paid_assets
    (root / "assets/nested.css").write_text(
        'body {background:image-set("free.png" 1x, "free%402.png" 2x)}',
        encoding="utf-8",
    )
    (root / "chapters/01.html").write_text(
        html("free", "<link rel=stylesheet href=../assets/shared.css>"),
        encoding="utf-8",
    )
    assert inspect_package(root).preview_assets == frozenset(
        {
            "assets/shared.css",
            "assets/nested.css",
            "assets/free.png",
            "assets/free@2.png",
        }
    )


def test_asset_closure_cannot_expose_a_paid_chapter_stored_under_assets(
    package_with_free_and_paid_assets,
):
    root = package_with_free_and_paid_assets
    data = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    data["chapters"][1]["path"] = "assets/paid.html"
    (root / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    (root / "assets/paid.html").write_text(html("paid chapter"), encoding="utf-8")
    (root / "chapters/01.html").write_text(
        html("<img src=../assets/paid.html>"), encoding="utf-8"
    )
    with pytest.raises(BusinessError):
        inspect_package(root)


@pytest.mark.parametrize("kind", ["directory", "symlink", "fifo"])
def test_zip_required_members_must_be_regular_files(fixture_package, kind):
    (fixture_package / "downloads").mkdir()
    with zipfile.ZipFile(fixture_package / "downloads/course.zip", "w") as archive:
        for relative in (
            "manifest.json",
            "index.html",
            "SOURCES.txt",
            "LICENSE.txt",
            "chapters/01.html",
        ):
            info = zipfile.ZipInfo(relative)
            if relative == "chapters/01.html":
                if kind == "directory":
                    info.filename += "/"
                else:
                    info.create_system = 3
                    mode = stat.S_IFLNK if kind == "symlink" else stat.S_IFIFO
                    info.external_attr = (mode | 0o644) << 16
            archive.writestr(info, (fixture_package / relative).read_bytes())
    assert inspect_package(fixture_package).zip_ready is False


@pytest.mark.parametrize(
    "body",
    [
        "<a href=#local ping=https://example.com/track>go</a>",
        "<img src=../assets/free.png lowsrc=https://example.com/p.png>",
        "<svg xml:base=https://example.com><use href=#local></use></svg>",
    ],
)
def test_other_url_attributes_cannot_bypass_safety(
    package_with_free_and_paid_assets, body
):
    root = package_with_free_and_paid_assets
    (root / "chapters/01.html").write_text(html(body), encoding="utf-8")
    with pytest.raises(BusinessError):
        inspect_package(root)


@pytest.mark.parametrize(
    "attribute",
    [
        "fill",
        "stroke",
        "filter",
        "clip-path",
        "mask",
        "marker",
        "marker-start",
        "marker-mid",
        "marker-end",
        "cursor",
    ],
)
def test_svg_presentation_urls_follow_resource_policy(
    package_with_free_and_paid_assets, attribute
):
    root = package_with_free_and_paid_assets
    chapter = root / "chapters/01.html"
    chapter.write_text(
        html(f'<svg><rect {attribute}="url(https://example.com/paint.svg#p)"/></svg>'),
        encoding="utf-8",
    )
    with pytest.raises(BusinessError):
        inspect_package(root)


@pytest.mark.parametrize(
    "attribute",
    [
        "fill",
        "stroke",
        "filter",
        "clip-path",
        "mask",
        "marker",
        "marker-start",
        "marker-mid",
        "marker-end",
        "cursor",
    ],
)
def test_svg_presentation_urls_include_transitive_preview_assets(
    package_with_free_and_paid_assets, attribute
):
    root = package_with_free_and_paid_assets
    (root / "assets/paint.svg").write_text(
        '<svg><image href="free.png"/></svg>', encoding="utf-8"
    )
    (root / "chapters/01.html").write_text(
        html(f'<svg><rect {attribute}="url(../assets/paint.svg#p)"/></svg>'),
        encoding="utf-8",
    )
    assert inspect_package(root).preview_assets == frozenset(
        {"assets/paint.svg", "assets/free.png"}
    )


@pytest.mark.parametrize(
    "animation",
    [
        '<set attributeName="href" to="https://example.com/image.png"/>',
        '<animate attributeName="xlink:href" values="free.png;https://example.com/image.png"/>',
        '<animate attributeName="fill" to="url(https://example.com/paint.svg#p)"/>',
    ],
)
def test_svg_url_mutating_animation_is_rejected(
    package_with_free_and_paid_assets, animation
):
    root = package_with_free_and_paid_assets
    (root / "chapters/01.html").write_text(
        html(f"<svg>{animation}</svg>"), encoding="utf-8"
    )
    with pytest.raises(BusinessError):
        inspect_package(root)


@pytest.mark.parametrize("suffix", ["SVG", "HTML"])
def test_uppercase_active_assets_are_rejected(
    package_with_free_and_paid_assets, suffix
):
    root = package_with_free_and_paid_assets
    asset = f"assets/active.{suffix}"
    body = (
        "<svg><script>steal()</script></svg>"
        if suffix == "SVG"
        else html("<script>steal()</script>")
    )
    (root / asset).write_text(body, encoding="utf-8")
    (root / "chapters/01.html").write_text(
        html(f'<img src="../{asset}">'), encoding="utf-8"
    )
    with pytest.raises(BusinessError):
        inspect_package(root)
    with pytest.raises(BusinessError):
        read_verified_file(root, asset, None)


def test_safe_uppercase_svg_keeps_fragment_and_transitive_dependencies(
    package_with_free_and_paid_assets,
):
    root = package_with_free_and_paid_assets
    (root / "assets/safe.SVG").write_text(
        '<svg><image href="free.png"/><rect fill="url(#local)"/></svg>',
        encoding="utf-8",
    )
    (root / "chapters/01.html").write_text(
        html('<img src="../assets/safe.SVG">'), encoding="utf-8"
    )
    assert inspect_package(root).preview_assets == frozenset(
        {"assets/safe.SVG", "assets/free.png"}
    )


def test_corrupt_deflate_zip_is_not_ready_and_does_not_block_verified_reads(
    fixture_package,
):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in (
            "manifest.json",
            "index.html",
            "SOURCES.txt",
            "LICENSE.txt",
            "chapters/01.html",
        ):
            archive.writestr(relative, (fixture_package / relative).read_bytes())
        offset = archive.getinfo("manifest.json").header_offset
    payload = bytearray(buffer.getvalue())
    # Local-file header: 30 fixed bytes, then filename and extra fields.
    name_length = int.from_bytes(payload[offset + 26 : offset + 28], "little")
    extra_length = int.from_bytes(payload[offset + 28 : offset + 30], "little")
    compressed_start = offset + 30 + name_length + extra_length
    payload[compressed_start] = 0x07  # final block, reserved DEFLATE block type 3
    (fixture_package / "downloads").mkdir()
    (fixture_package / "downloads/course.zip").write_bytes(payload)
    try:
        inspection = inspect_package(fixture_package)
    except zlib.error as exc:
        pytest.fail(f"corrupt ZIP escaped the readiness boundary: {exc}")
    assert inspection.zip_ready is False
    assert (
        read_verified_file(fixture_package, "LICENSE.txt", inspection.fingerprint)
        == (fixture_package / "LICENSE.txt").read_bytes()
    )
