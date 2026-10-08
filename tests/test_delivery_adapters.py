import hashlib
from contextlib import closing

import pytest

from course_platform.access import AccessService
from course_platform.content import CourseManifest
from course_platform.database import open_readonly, sync_course
from course_platform.domain import BusinessError


def test_adapter_requires_actor_and_sale_ready_product(db_path, active_product, actor):
    service = AccessService(db_path)
    with pytest.raises(TypeError):
        service.create_access_code("fixture-course")
    raw = service.create_access_code("fixture-course", actor=actor, activation_days=30)
    assert len(raw) == 35
    receipt = service.redeem_access_code(raw, "fixture-course")
    assert receipt.raw_recovery_key.startswith("LK-")
    assert service.get_session(receipt.session.session_id).entitlement_id == receipt.session.entitlement_id


def test_resync_rejects_changes_preserving_release_and_progress(db_path, fixture_package, active_product, redeemed, progress_service):
    manifest = CourseManifest.model_validate_json((fixture_package / "manifest.json").read_bytes())
    progress_service.set_completed(redeemed.session.session_id, manifest.course_id, 1, True)
    with closing(open_readonly(db_path)) as conn:
        before = dict(conn.execute("SELECT * FROM courses").fetchone())
    changed = manifest.model_copy(update={"version": "0.1.1"})
    with pytest.raises(BusinessError):
        sync_course(changed, fixture_package, db_path)
    sync_course(manifest, fixture_package, db_path)
    with closing(open_readonly(db_path)) as conn:
        assert dict(conn.execute("SELECT * FROM courses").fetchone()) == before
    assert progress_service.get_progress(redeemed.session.session_id, manifest.course_id) == {1: True}


def test_cli_create_code_requires_owner_and_sale_ready_product(db_path, fixture_package, active_product, actor, monkeypatch, admin_settings, capsys):
    from dataclasses import replace
    from course_platform import cli
    monkeypatch.setattr(cli, "load_settings", lambda: replace(admin_settings, content_root=fixture_package.parent))
    monkeypatch.setattr("builtins.input", lambda _: "owner")
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "wrong-password")
    assert cli.main(["create-code", "fixture-course"]) == 1
    assert "CS-" not in capsys.readouterr().out
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "example-pass-123")
    assert cli.main(["create-code", "fixture-course"]) == 0
    raw = capsys.readouterr().out.strip()
    assert raw.startswith("CS-") and len(raw) == 35
    with closing(open_readonly(db_path)) as conn:
        assert conn.execute("SELECT code_hash FROM access_codes").fetchone()[0] == hashlib.sha256(raw.encode()).hexdigest()


def test_cli_serve_factory_preserves_socket_peer(monkeypatch, admin_settings):
    import uvicorn
    from course_platform import cli
    calls = []
    monkeypatch.setattr(cli, "load_settings", lambda: admin_settings)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    assert cli.main(["serve"]) == 0
    assert calls[0][0] == ("course_platform.app:create_app",)
    assert calls[0][1]["factory"] is True and calls[0][1]["proxy_headers"] is False


def test_cli_publish_duplicate_id_does_not_copy_files(db_path, active_product, fixture_package, monkeypatch, admin_settings, capsys):
    from course_platform import cli
    monkeypatch.setattr(cli, "load_settings", lambda: admin_settings)
    assert not admin_settings.content_root.exists()
    assert cli.main(["publish", str(fixture_package)]) == 1
    assert not admin_settings.content_root.exists()
    assert "publish_unavailable" in capsys.readouterr().out


@pytest.mark.parametrize("days", [True, 0, 366, "30", 30.0])
def test_adapter_never_mints_policyless_or_invalid_deadline_codes(db_path, active_product, actor, days):
    service = AccessService(db_path)
    with pytest.raises(BusinessError):
        service.create_access_code("fixture-course", actor=actor, activation_days=days)
    with closing(open_readonly(db_path)) as connection:
        assert connection.execute("SELECT count(*) FROM access_codes").fetchone()[0] == 0


def test_adapter_paused_product_denies_new_mint(db_path, active_product, actor, product_service):
    product_service.set_status(actor, active_product.id, active_product.revision, "paused")
    with pytest.raises(BusinessError):
        AccessService(db_path).create_access_code("fixture-course", actor=actor)
