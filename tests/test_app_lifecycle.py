"""Production factory boundaries, without a production database."""
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from course_platform.app import create_app
from course_platform.database import check_database, initialize_database
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
