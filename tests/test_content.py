import json
import shutil
from pathlib import Path

import pytest

from course_platform.content import (
    load_course_package,
    publish_course,
    validate_course_package,
)


@pytest.fixture
def fixture_package(tmp_path):
    source = Path(__file__).parent / "fixtures" / "course-package"
    target = tmp_path / "fixture-course"
    shutil.copytree(source, target)
    return target


def test_valid_package_loads(fixture_package):
    package = load_course_package(fixture_package)
    assert package.manifest.slug == "fixture-course"
    assert package.chapter_files == [fixture_package / "chapters" / "01.html"]


def test_missing_chapter_blocks_publish(tmp_path, fixture_package):
    manifest = json.loads((fixture_package / "manifest.json").read_text(encoding="utf-8"))
    manifest["chapter_count"] = 2
    manifest["chapters"].append(
        {"number": 2, "title": "第二章", "path": "chapters/02.html", "free_preview": False}
    )
    (fixture_package / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )

    report = validate_course_package(fixture_package)

    assert report.ok is False
    assert "chapters/02.html" in report.errors[0]


def test_publish_does_not_overwrite_existing_slug(tmp_path, fixture_package):
    target = tmp_path / "content" / "fixture-course"
    target.mkdir(parents=True)

    with pytest.raises(FileExistsError):
        publish_course(
            fixture_package,
            tmp_path / "content",
            load_course_package(fixture_package).manifest,
        )


def test_manifest_rejects_parent_path(fixture_package):
    data = json.loads((fixture_package / "manifest.json").read_text(encoding="utf-8"))
    data["chapters"][0]["path"] = "../secrets.html"
    (fixture_package / "manifest.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )

    report = validate_course_package(fixture_package)

    assert report.ok is False


def test_external_resources_block_publish(fixture_package):
    html_path = fixture_package / "chapters" / "01.html"
    html_path.write_text(
        '<!doctype html><html><head><link rel="stylesheet" href="https://example.com/x.css">'
        '</head><body><img src="https://example.com/x.png"></body></html>',
        encoding="utf-8",
    )

    report = validate_course_package(fixture_package)

    assert report.ok is False
    assert any("external" in error for error in report.errors)


def test_inline_script_and_event_handler_block_publish(fixture_package):
    html_path = fixture_package / "chapters" / "01.html"
    html_path.write_text(
        "<!doctype html><html><head><title>unsafe</title></head>"
        '<body onload="steal()"><script>steal()</script></body></html>',
        encoding="utf-8",
    )

    report = validate_course_package(fixture_package)

    assert report.ok is False
    assert any("script elements" in error for error in report.errors)
    assert any("event handlers" in error for error in report.errors)


def test_free_chapter_fields_must_agree(fixture_package):
    data = json.loads((fixture_package / "manifest.json").read_text(encoding="utf-8"))
    data["chapters"][0]["free_preview"] = False
    (fixture_package / "manifest.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )

    report = validate_course_package(fixture_package)

    assert report.ok is False
    assert "free_chapters" in report.errors[0]
