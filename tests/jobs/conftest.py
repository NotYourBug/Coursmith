"""Real database fixtures; new job APIs load from test bodies for genuine RED."""

from contextlib import closing
from types import SimpleNamespace

import pytest

from course_platform.database import open_readonly
from course_platform.production.drafts import DraftService
from course_platform.production.models import BriefInput, OutlineInput, OutlineItem
from course_platform.production.projects import ProjectService


@pytest.fixture
def jobs_context(db_path, clock, product_owner):
    projects = ProjectService(db_path, clock=clock.now)
    drafts = DraftService(db_path, clock=clock.now)

    def load():
        from course_platform.jobs.models import JobRequest
        from course_platform.jobs.repository import JobRepository
        from course_platform.jobs.service import JobService
        from course_platform.jobs.budget import TokenBudget
        from tests.jobs.fakes import production_settings

        execution_config = production_settings(db_path.parent)
        config = execution_config.public
        budget = TokenBudget(db_path, clock=clock.now)
        service = JobService(db_path, drafts, clock=clock.now)
        service.configure_execution(execution_config, budget)
        repository = JobRepository(db_path, clock=clock.now)

        def project(*, mode="quick", outline=False):
            p = projects.create(product_owner, BriefInput(title="private-title-中文😀", chapter_count=2), mode=mode)
            if outline:
                drafts.save_outline(product_owner, p.id, p.revision, OutlineInput(items=[
                    OutlineItem(chapter_id=None, title="第一章", goal="private-goal"),
                    OutlineItem(chapter_id=None, title="第二章", goal="目标")]))
                p = projects.get(p.id)
            return p

        def submit(p=None, *, kind="course_draft_generate", key="test-key", chapter_ids=(), reserved_tokens=100):
            return service.submit(product_owner, JobRequest(kind=kind, project_id=p.id if p else None,
                project_revision=p.revision if p else None, chapter_ids=chapter_ids),
                config, idempotency_key=key, reserved_tokens=reserved_tokens)

        return SimpleNamespace(service=service, repository=repository, projects=projects, drafts=drafts,
            actor=product_owner, config=config, execution_config=execution_config, budget=budget, request=JobRequest, project=project, submit=submit,
            db_path=db_path, clock=clock, repository_type=JobRepository)
    return load


@pytest.fixture
def read_rows(db_path):
    def read(sql, params=()):
        with closing(open_readonly(db_path)) as connection:
            return [dict(row) for row in connection.execute(sql, params)]
    return read



@pytest.fixture
def job_repository(db_path, clock):
    from course_platform.jobs.repository import JobRepository

    return JobRepository(db_path, clock=clock.now)


@pytest.fixture
def claimed_job(jobs_context, job_repository):
    context = jobs_context()
    context.submit(context.project())
    return job_repository.claim_next("fixture-worker")
