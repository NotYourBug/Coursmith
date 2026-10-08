from pathlib import Path

import course_gen_core


def test_incomplete_course_is_retained_for_recovery(tmp_path):
    course_dir = tmp_path / "category" / "unfinished-course"
    course_dir.mkdir(parents=True)
    (course_dir / "01.html").write_text("<!doctype html>", encoding="utf-8")

    passed, failed = course_gen_core.check_and_clean_incomplete_courses(
        str(tmp_path), expected_lessons=2
    )

    assert passed == []
    assert failed == [(str(course_dir), 1)]
    assert course_dir.is_dir()


def test_batch_pipeline_processes_only_explicit_files(tmp_path, monkeypatch):
    selected = tmp_path / "selected.txt"
    ignored = tmp_path / "ignored.txt"
    selected.write_text("课程 A\n", encoding="utf-8")
    ignored.write_text("课程 B\n", encoding="utf-8")
    processed = []

    monkeypatch.setattr(course_gen_core, "load_config", lambda: None)
    monkeypatch.setattr(
        course_gen_core,
        "process_course_file",
        lambda filepath, *_args: processed.append(Path(filepath)),
    )
    monkeypatch.setattr(
        course_gen_core,
        "check_and_clean_incomplete_courses",
        lambda *_args, **_kwargs: ([], []),
    )

    course_gen_core.run_batch_pipeline(
        input_dir=str(tmp_path),
        lessons_per_course=1,
        max_concurrent_files=1,
        do_png=False,
        do_zip=False,
        input_files=[selected],
    )

    assert processed == [selected.resolve()]
