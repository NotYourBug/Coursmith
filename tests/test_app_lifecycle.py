"""Production factory boundaries, without a production database."""
import os
import json
import subprocess
import sys
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from course_platform.app import create_app
from course_platform.content import CourseManifest
from course_platform.content_inspection import inspect_package
from course_platform.database import check_database, initialize_database, open_readonly, sync_course
from course_platform.domain import BusinessError
from course_platform.settings import Settings


@pytest.mark.parametrize("existing", [False, True])
def test_importing_app_does_not_touch_database(tmp_path, existing):
    db = tmp_path / "data" / "course.db"
    content = tmp_path / "content"
    if existing:
        initialize_database(db)
    before = (db.read_bytes(), db.stat().st_mtime_ns) if existing else None
    result = subprocess.run([sys.executable, "-c", "import course_platform.app"],
        env={**os.environ, "COURSE_DATABASE": str(db), "COURSE_CONTENT_ROOT": str(content)},
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not content.exists()
    if existing:
        assert (db.read_bytes(), db.stat().st_mtime_ns) == before
    else:
        assert not db.exists() and not db.parent.exists()


def test_lifespan_initializes_latest_and_imports_owner_free_drafts(tmp_path, fixture_package):
    db = tmp_path / "new.db"
    settings = Settings("", "", fixture_package.parent, db, 72, "test", "http://testserver")
    app = create_app(settings)
    assert not db.exists()
    with TestClient(app) as client:
        assert check_database(db)["version"] == 5
        product = app.state.product_service.list_products(status="draft", category_id=None, title="", page=1)[0][0]
        assert product.status == "draft"
        assert client.get("/courses/fixture-course").status_code == 404


def test_existing_old_database_refuses_startup_without_mutation(tmp_path):
    db = tmp_path / "old.db"
    initialize_database(db)
    before = db.read_bytes()
    app = create_app(Settings("", "", tmp_path / "content", db, 72, "test"))
    with pytest.raises(BusinessError, match="offline|backup|migrat"):
        with TestClient(app):
            pass
    assert db.read_bytes() == before


def test_factory_never_falls_back_to_paths_outside_configured_content_root(db_path, active_product, tmp_path):
    # A copied DB retaining original absolute paths must fail closed, even
    # while those original bytes remain present and independently valid.
    app = create_app(Settings("", "", tmp_path / "restored-content", db_path, 72, "test"))
    with pytest.raises(BusinessError, match="relocat|content root"):
        with TestClient(app):
            pass


def test_nonascending_release_resync_and_restart_preserve_original_baseline(tmp_path, delivery_content):
    path = delivery_content / "manifest.json"
    data = json.loads(path.read_bytes())
    data["chapters"].reverse()
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf8")
    manifest = CourseManifest.model_validate_json(path.read_bytes())
    assert [chapter.number for chapter in manifest.chapters] == [2, 1]
    original_bytes = {p.relative_to(delivery_content).as_posix(): p.read_bytes()
                      for p in delivery_content.rglob("*") if p.is_file()}
    fingerprint = inspect_package(delivery_content).fingerprint
    db = tmp_path / "nonascending.db"
    settings = Settings("", "", delivery_content.parent, db, 72, "test", "http://testserver")

    def stored_baseline():
        with closing(open_readonly(db)) as connection:
            return (dict(connection.execute("SELECT * FROM courses").fetchone()),
                    [tuple(row) for row in connection.execute("SELECT * FROM chapters ORDER BY chapter_number")])

    with TestClient(create_app(settings)) as client:
        assert client.get("/courses/fixture-course").status_code == 404  # Honest imported draft.
    baseline = stored_baseline()
    assert baseline[0]["package_hash"] == fingerprint
    sync_course(manifest, delivery_content, db)
    for _ in range(2):
        with TestClient(create_app(settings)) as client:
            assert client.get("/help").status_code == 200
        assert stored_baseline() == baseline
    assert CourseManifest.model_validate_json(path.read_bytes()) == manifest
    assert {p.relative_to(delivery_content).as_posix(): p.read_bytes()
            for p in delivery_content.rglob("*") if p.is_file()} == original_bytes

    # Mapping comparison must not permit a real on-disk order/version change:
    # the original fingerprint remains the release-order/byte baseline.
    for change in ("order", "version", "chapter"):
        changed = json.loads(original_bytes["manifest.json"])
        if change == "order":
            changed["chapters"].reverse()
        elif change == "version":
            changed["version"] = "0.1.1"
        else:
            changed["chapters"][0]["title"] = "Changed chapter"
        candidate = json.dumps(changed, ensure_ascii=False).encode("utf8")
        path.write_bytes(candidate)
        try:
            with pytest.raises(BusinessError) as denied:
                sync_course(CourseManifest.model_validate_json(candidate), delivery_content, db)
            assert denied.value.code == "release_exists"
            assert path.read_bytes() == candidate  # No production rewrite to force equality.
            assert stored_baseline() == baseline
        finally:
            path.write_bytes(original_bytes["manifest.json"])
    assert inspect_package(delivery_content).fingerprint == fingerprint
    assert {p.relative_to(delivery_content).as_posix(): p.read_bytes()
            for p in delivery_content.rglob("*") if p.is_file()} == original_bytes
