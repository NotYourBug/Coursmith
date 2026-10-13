"""Known private text is permitted only in private source/backup storage."""

import asyncio
import logging
from contextlib import closing
from pathlib import Path

from course_platform.database import backup_database, open_readonly
from course_platform.jobs.budget import TokenBudget
from course_platform.jobs.models import JobRequest
from course_platform.jobs.repository import JobRepository
from course_platform.jobs.service import JobService
from course_platform.jobs.worker import Worker
from course_platform.production.llm import ModelFailure
from course_platform.production.models import ReferenceInput
from tests.jobs.fakes import ScriptedModel, production_settings
from tests.production.test_snapshots import BODY, ready


SOURCE_SENTINEL = "TASK13_PRIVATE_SOURCE_4dd44cae_中文😀"
KEY_SENTINEL = "sk-task13-private-credential-781f782f"


def test_logs_events_and_artifacts_do_not_contain_known_secret_sentinels(db_path, clock, product_owner, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    r = ready(db_path, clock, product_owner, count=1)
    r.projects.save_reference(product_owner, r.project_id, r.projects.get(r.project_id).revision, None,
        ReferenceInput(text=SOURCE_SENTINEL, source_note="自有测试资料", source_status="owner_noted"))
    r.drafts.save_chapter(product_owner, r.project_id, r.chapter_id, r.body_revision, BODY + SOURCE_SENTINEL)
    r.drafts.acknowledge_changes(product_owner, r.project_id, r.projects.get(r.project_id).revision, (r.chapter_id,))
    snapshot = r.snapshots.create(product_owner, r.project_id, r.projects.get(r.project_id).revision)
    config = production_settings(tmp_path, api_key=KEY_SENTINEL)
    budget = TokenBudget(db_path, clock=clock.now)
    jobs = JobService(db_path, r.drafts, clock=clock.now)
    jobs.configure_execution(config, budget)
    job = jobs.submit(product_owner, JobRequest(kind="chapter_regenerate", project_id=r.project_id,
        project_revision=r.projects.get(r.project_id).revision, chapter_ids=(r.chapter_id,)), config.public,
        idempotency_key="privacy-regenerate", reserved_tokens=0)
    model = ScriptedModel([ModelFailure("response_unknown", accepted=None)])
    worker = Worker(JobRepository(db_path, clock=clock.now), r.drafts, budget, model, config,
                    worker_id="privacy-local", clock=clock.now)
    assert asyncio.run(worker.run_once()) and jobs.get(job.id).state == "failed"
    assert len(model.calls) == 1
    assert SOURCE_SENTINEL in snapshot.payload_json and KEY_SENTINEL not in repr(config)
    backup = tmp_path / "private-backup.db"
    backup_database(db_path, backup)
    with closing(open_readonly(db_path)) as connection:
        assert connection.execute("SELECT status FROM model_calls").fetchone()[0] == "unknown"
        assert connection.execute("SELECT sum(reserved_tokens) FROM token_reservations WHERE call_id IS NOT NULL").fetchone()[0] > 0
        artifacts = caplog.text + repr(config.public)
        for table in ("admin_events", "events", "model_calls", "token_reservations", "worker_heartbeats", "job_requests"):
            artifacts += repr([tuple(row) for row in connection.execute(f"SELECT * FROM {table}")])
        artifacts += repr([tuple(row) for row in connection.execute("SELECT config_json,reason_code FROM production_jobs")])
        # Frozen input JSON is private production source, never a public/log artifact.
        all_values = repr([tuple(row) for name in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                          for row in connection.execute(f'SELECT * FROM "{name[0]}"')])
    assert SOURCE_SENTINEL not in artifacts and KEY_SENTINEL not in artifacts
    assert KEY_SENTINEL not in all_values
    with closing(open_readonly(backup)) as connection:
        assert SOURCE_SENTINEL in connection.execute("SELECT payload_json FROM production_snapshots").fetchone()[0]
    assert not list(tmp_path.rglob("*.html")), "Private source must not become public artifacts"
