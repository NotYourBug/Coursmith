import pytest

from course_platform.settings import Settings, load_settings


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


def test_environment_name_is_normalized_for_security_checks(tmp_path):
    settings = load_settings(
        {
            "COURSE_ENVIRONMENT": " Production ",
            "COURSE_SITE_ORIGIN": "https://courses.example",
            "COURSE_CONTENT_ROOT": str(tmp_path / "content"),
            "COURSE_DATABASE": str(tmp_path / "course.db"),
        }
    )

    assert settings.environment == "production"


def test_site_origin_defaults_are_independent_of_llm_url(tmp_path, monkeypatch):
    monkeypatch.setattr("course_platform.settings.REPOSITORY_ROOT", tmp_path)
    monkeypatch.delenv("COURSE_SITE_ORIGIN", raising=False)
    monkeypatch.delenv("COURSE_TRUSTED_PROXY_CIDRS", raising=False)
    settings = load_settings({"COURSE_ENVIRONMENT": "development", "COURSE_BASE_URL": "https://api.deepseek.com/"})
    assert getattr(settings, "site_origin", None) == "http://127.0.0.1:8000"
    assert settings.trusted_proxy_cidrs == ()
    assert settings.base_url == "https://api.deepseek.com/"
    # The original six positional fields still construct valid public-app settings.
    original = Settings("https://api.deepseek.com/", "secret", tmp_path, tmp_path / "db", 72, "development")
    assert original.site_origin == "http://127.0.0.1:8000"
    assert "secret" not in str(original.to_public_dict())


def test_explicit_origin_and_proxy_networks_are_loaded(tmp_path, monkeypatch):
    monkeypatch.setattr("course_platform.settings.REPOSITORY_ROOT", tmp_path)
    settings = load_settings({
        "COURSE_ENVIRONMENT": "production", "COURSE_SITE_ORIGIN": "https://courses.example",
        "COURSE_TRUSTED_PROXY_CIDRS": "10.0.0.0/8, 2001:db8::/32",
        "DEEPSEEK_API_KEY": "secret-value",
    })
    assert getattr(settings, "site_origin", None) == "https://courses.example"
    assert settings.trusted_proxy_cidrs == ("10.0.0.0/8", "2001:db8::/32")
    assert "secret-value" not in str(settings.to_public_dict())


@pytest.mark.parametrize("origin", [None, "", "http://courses.example"])
def test_production_requires_explicit_https_origin(tmp_path, monkeypatch, origin):
    monkeypatch.setattr("course_platform.settings.REPOSITORY_ROOT", tmp_path)
    monkeypatch.delenv("COURSE_SITE_ORIGIN", raising=False)
    values = {"COURSE_ENVIRONMENT": "production"}
    if origin is not None:
        values["COURSE_SITE_ORIGIN"] = origin
    with pytest.raises(ValueError, match="COURSE_SITE_ORIGIN"):
        load_settings(values)


@pytest.mark.parametrize("origin", [
    "https://courses.example/path", "https://user:pass@courses.example", "null",
    "https://courses.example?x=1", "https://courses.example#x", "https://courses.example/",
    "https://courses.example:invalid", "https://courses.example https://evil.example",
])
def test_invalid_site_origins_are_rejected(tmp_path, monkeypatch, origin):
    monkeypatch.setattr("course_platform.settings.REPOSITORY_ROOT", tmp_path)
    with pytest.raises(ValueError, match="COURSE_SITE_ORIGIN"):
        load_settings({"COURSE_SITE_ORIGIN": origin, "COURSE_ENVIRONMENT": "development"})


@pytest.mark.parametrize("cidrs", ["garbage", "10.0.0.0/99", "10.0.0.0/8,,127.0.0.0/8"])
def test_invalid_proxy_networks_are_rejected(tmp_path, monkeypatch, cidrs):
    monkeypatch.setattr("course_platform.settings.REPOSITORY_ROOT", tmp_path)
    with pytest.raises(ValueError, match="COURSE_TRUSTED_PROXY_CIDRS"):
        load_settings({"COURSE_TRUSTED_PROXY_CIDRS": cidrs, "COURSE_ENVIRONMENT": "development"})


@pytest.mark.parametrize("origin", ["https://[2001:db8::1]evil", "https://["])
def test_malformed_ipv6_configuration_is_rejected_safely(tmp_path, monkeypatch, origin):
    monkeypatch.setattr("course_platform.settings.REPOSITORY_ROOT", tmp_path)
    with pytest.raises(ValueError, match="COURSE_SITE_ORIGIN"):
        load_settings({"COURSE_SITE_ORIGIN": origin, "COURSE_ENVIRONMENT": "development"})
