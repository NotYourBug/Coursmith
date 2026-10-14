"""Fixed worker command boundaries through an isolated real Python process."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from course_platform.database import initialize_database, migrate_database
from tests.jobs.fakes import ProcessHarness, isolated_process_env, wait_until


def cli_process(tmp_path, arguments, *, database=None, configured=True, module="course_platform.cli", env_changes=None):
    db = database or tmp_path / "missing" / "test.db"
    env = isolated_process_env(tmp_path, db, tmp_path / "content", configured=configured)
    env.update(env_changes or {})
    script = "import course_platform.settings as s; s._read_dotenv=lambda _: {}; import runpy; runpy.run_module('" + module + "',run_name='__main__')"
    return subprocess.run([sys.executable, "-X", "utf8", "-c", script, *arguments],
        cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=10, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


@pytest.fixture
def worker_help_result(tmp_path):
    return cli_process(tmp_path, ["worker", "--help"])


def test_worker_help_uses_fixed_entrypoint(worker_help_result):
    assert worker_help_result.returncode == 0, worker_help_result.stderr
    assert "worker" in worker_help_result.stdout
    assert "--api-key" not in worker_help_result.stdout
    assert "--shell" not in worker_help_result.stdout


@pytest.mark.parametrize("existing", [False, True])
def test_worker_cli_import_and_help_do_not_start_or_migrate(tmp_path, existing):
    db = tmp_path / "data" / "test.db"
    if existing:
        initialize_database(db)
    before = db.read_bytes() if existing else None
    env = isolated_process_env(tmp_path, db, tmp_path / "content")
    imported = subprocess.run([sys.executable, "-X", "utf8", "-c",
        "import course_platform.cli; import course_platform.jobs.worker"], env=env,
        cwd=Path(__file__).resolve().parents[2], capture_output=True, timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert imported.returncode == 0, imported.stderr
    for module, args in (("course_platform.cli", ["worker", "--help"]),
                         ("course_platform.jobs.worker", ["--help"])):
        result = cli_process(tmp_path, args, database=db, module=module)
        assert result.returncode == 0, result.stderr
        assert "worker" in result.stdout
    assert not (tmp_path / "content").exists()
    if existing:
        assert db.read_bytes() == before
    else:
        assert not db.exists() and not db.parent.exists()


@pytest.mark.parametrize("option", ["--api-key", "--shell", "--module", "--path", "--worker-id"])
def test_worker_rejects_secret_or_arbitrary_execution_arguments_privately(tmp_path, option):
    sentinel = "task12-private-argv-sentinel"
    result = cli_process(tmp_path, ["worker", option, sentinel])
    assert result.returncode == 2
    assert sentinel not in result.stdout + result.stderr
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("state,code", [("missing", "offline_migration_required"),
    ("old", "offline_migration_required"), ("no_owner", "owner_required"), ("disabled", "owner_required")])
def test_worker_explicit_start_refuses_unready_database_without_mutation(tmp_path, state, code):
    from course_platform.admin.auth import AdminService
    from course_platform.database import transaction

    db = tmp_path / "startup.db"
    if state == "old":
        migrate_database(db, through_version=5)
    elif state != "missing":
        migrate_database(db)
        if state == "disabled":
            AdminService(db).initialize_owner("owner", "example-pass-123")
            with transaction(db, immediate=True) as conn:
                conn.execute("UPDATE admins SET enabled=0 WHERE id=1")
    before = db.read_bytes() if db.exists() else None
    result = cli_process(tmp_path, ["worker"], database=db)
    assert result.returncode == 1, result.stderr
    assert code in result.stdout
    assert "task12-fake-key" not in result.stdout + result.stderr
    assert (db.read_bytes() if db.exists() else None) == before


def test_worker_main_is_explicit_fixed_entrypoint():
    import course_platform.jobs.worker as worker
    assert callable(getattr(worker, "main", None)), "jobs.worker.main(argv=None)->int is required"


def test_missing_model_diagnosis_blocks_generation_without_new_call_or_reservation(tmp_path, jobs_context):
    c = jobs_context()
    job = c.submit(c.project())
    h = ProcessHarness(tmp_path, c.db_path, c.actor, c.clock)
    try:
        before = h.rows("SELECT id,call_id,reserved_tokens FROM token_reservations")
        h.env["DEEPSEEK_API_KEY"] = ""
        h.env["COURSE_MODEL_CONTEXT_TOKENS"] = ""
        supplier = h.supplier([])
        process = h.spawn("worker", supplier)
        assert wait_until(lambda: process.poll() is not None or "worker_ready" in process.task12_log.read_text(encoding="utf-8"))
        assert process.poll() is None, process.task12_log.read_text(encoding="utf-8")
        assert wait_until(lambda: c.service.get(job.id).state == "failed")
        assert c.service.get(job.id).reason_code == "model_config_changed"
        assert h.rows("SELECT * FROM model_calls") == []
        assert h.rows("SELECT id,call_id,reserved_tokens FROM token_reservations") == before
        assert len(supplier.requests) == 0
        diagnostic = process.task12_log.read_text(encoding="utf-8")
        assert "model_context_missing" in diagnostic and "local_only" in diagnostic
        assert "task12-fake-key" not in diagnostic
        h.signal(process)
        assert wait_until(lambda: process.poll() is not None)
        assert process.returncode == 0
    finally:
        h.close()


@pytest.mark.parametrize("env_changes", [{"COURSE_MODEL": "deepseek-chat"},
    {"COURSE_BASE_URL": "https://unsupported.example/"}])
@pytest.mark.parametrize("configured", [False, True])
def test_missing_credentials_do_not_hide_unsupported_model_profile(tmp_path, db_path, product_owner, env_changes, configured):
    result = cli_process(tmp_path, ["worker"], database=db_path, configured=configured, env_changes=env_changes)
    assert result.returncode == 1
    assert "model_output_unsupported" in result.stdout
    assert "worker_ready" not in result.stdout and "task12-fake-key" not in result.stdout + result.stderr
