from course_platform.settings import load_settings


def test_environment_key_is_used_without_serializing_it(tmp_path):
    settings = load_settings(
        {
            "DEEPSEEK_API_KEY": "secret-value",
            "COURSE_CONTENT_ROOT": str(tmp_path / "content"),
            "COURSE_DATABASE": str(tmp_path / "course.db"),
        }
    )

    assert settings.api_key == "secret-value"
    assert "secret-value" not in settings.to_public_dict().__str__()
    assert settings.has_llm_credentials is True


def test_missing_key_is_explicitly_reported(tmp_path):
    settings = load_settings(
        {
            "COURSE_CONTENT_ROOT": str(tmp_path / "content"),
            "COURSE_DATABASE": str(tmp_path / "course.db"),
        }
    )

    assert settings.api_key == ""
    assert settings.has_llm_credentials is False


def test_relative_runtime_paths_are_created(tmp_path, monkeypatch):
    monkeypatch.setattr("course_platform.settings.REPOSITORY_ROOT", tmp_path)

    settings = load_settings(
        {
            "COURSE_CONTENT_ROOT": "content/courses",
            "COURSE_DATABASE": "data/course.db",
        }
    )

    assert settings.content_root == tmp_path / "content" / "courses"
    assert settings.database_path == tmp_path / "data" / "course.db"
    assert settings.content_root.is_dir()
    assert settings.database_path.parent.is_dir()


def test_non_positive_session_ttl_is_rejected(tmp_path):
    try:
        load_settings(
            {
                "COURSE_SESSION_TTL_HOURS": "0",
                "COURSE_CONTENT_ROOT": str(tmp_path / "content"),
                "COURSE_DATABASE": str(tmp_path / "course.db"),
            }
        )
    except ValueError as exc:
        assert "greater than zero" in str(exc)
    else:
        raise AssertionError("expected invalid session TTL to be rejected")
