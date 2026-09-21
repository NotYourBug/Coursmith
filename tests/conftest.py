import shutil
from pathlib import Path

import pytest


@pytest.fixture
def fixture_package(tmp_path):
    source = Path(__file__).parent / "fixtures" / "course-package"
    target = tmp_path / "fixture-course"
    shutil.copytree(source, target)
    return target
