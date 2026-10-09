"""Local production service fixtures; database and owner stay shared with M1."""

import pytest


@pytest.fixture
def project_service(db_path, clock):
    from course_platform.production.projects import ProjectService

    return ProjectService(db_path, clock=clock.now)
