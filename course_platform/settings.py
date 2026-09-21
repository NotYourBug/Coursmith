"""Runtime configuration with safe, non-secret defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

try:
    from dotenv import dotenv_values
except ImportError:  # pragma: no cover - fallback keeps first boot usable
    dotenv_values = None


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    if dotenv_values is not None:
        return {
            key: value
            for key, value in dotenv_values(path).items()
            if value is not None
        }

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _resolve_path(value: str, repository_root: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else repository_root / path


@dataclass(frozen=True)
class Settings:
    base_url: str
    api_key: str
    content_root: Path
    database_path: Path
    session_ttl_hours: int
    environment: str

    @property
    def has_llm_credentials(self) -> bool:
        return bool(self.api_key.strip())

    def to_public_dict(self) -> dict[str, object]:
        """Return safe diagnostics; secrets are intentionally excluded."""
        return {
            "base_url": self.base_url,
            "content_root": str(self.content_root),
            "database_path": str(self.database_path),
            "session_ttl_hours": self.session_ttl_hours,
            "environment": self.environment,
            "has_llm_credentials": self.has_llm_credentials,
        }


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Load settings using explicit values, process env, then ``.env``.

    Relative paths are resolved from the repository root. Runtime directories
    are created here so later services can rely on their existence.
    """
    dotenv = _read_dotenv(REPOSITORY_ROOT / ".env")
    values: dict[str, str] = dict(dotenv)
    values.update({key: value for key, value in os.environ.items()})
    if env is not None:
        values.update({key: str(value) for key, value in env.items()})

    raw_ttl = values.get("COURSE_SESSION_TTL_HOURS", "72")
    try:
        session_ttl_hours = int(raw_ttl)
    except (TypeError, ValueError) as exc:
        raise ValueError("COURSE_SESSION_TTL_HOURS must be an integer") from exc
    if session_ttl_hours <= 0:
        raise ValueError("COURSE_SESSION_TTL_HOURS must be greater than zero")

    content_root = _resolve_path(
        values.get("COURSE_CONTENT_ROOT", "content/courses"), REPOSITORY_ROOT
    )
    database_path = _resolve_path(
        values.get("COURSE_DATABASE", "data/course.db"), REPOSITORY_ROOT
    )
    content_root.mkdir(parents=True, exist_ok=True)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    return Settings(
        base_url=values.get("COURSE_BASE_URL", "https://api.deepseek.com/").strip(),
        api_key=values.get("DEEPSEEK_API_KEY", "").strip(),
        content_root=content_root,
        database_path=database_path,
        session_ttl_hours=session_ttl_hours,
        environment=values.get("COURSE_ENVIRONMENT", "development").strip()
        or "development",
    )
