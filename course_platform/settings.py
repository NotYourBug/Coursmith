"""Runtime configuration with safe, non-secret defaults."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

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


def _origin_tuple(value: str) -> tuple[str, str, int]:
    """Parse only a serialized HTTP origin, never a URL or a host header."""
    if not value or any(char.isspace() or ord(char) < 32 for char in value) or any(
        char in value for char in "\\,%?#"
    ):
        raise ValueError("COURSE_SITE_ORIGIN must be an HTTP(S) origin without a path")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ValueError("COURSE_SITE_ORIGIN has an invalid authority") from None
    host = parsed.hostname
    if (parsed.scheme not in ("http", "https") or not host or parsed.path
            or parsed.username is not None or parsed.password is not None
            or not re.fullmatch(r"(?:\[[0-9a-fA-F:.]+\]|[a-zA-Z0-9.-]+)(?::[0-9]+)?", parsed.netloc)):
        raise ValueError("COURSE_SITE_ORIGIN must be an HTTP(S) origin without a path")
    try:
        host = str(ip_address(host))
    except ValueError:
        if len(host) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                      for label in host.split(".")):
            raise ValueError("COURSE_SITE_ORIGIN has an invalid hostname") from None
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("COURSE_SITE_ORIGIN has an invalid port") from None
    if port == 0:
        raise ValueError("COURSE_SITE_ORIGIN has an invalid port")
    return parsed.scheme, host, port if port is not None else (443 if parsed.scheme == "https" else 80)


@dataclass(frozen=True)
class Settings:
    base_url: str
    api_key: str
    content_root: Path
    database_path: Path
    session_ttl_hours: int
    environment: str
    site_origin: str = "http://127.0.0.1:8000"
    trusted_proxy_cidrs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        scheme, _, _ = _origin_tuple(self.site_origin)
        if self.environment == "production" and scheme != "https":
            raise ValueError("COURSE_SITE_ORIGIN must be an explicit HTTPS origin in production")
        try:
            for cidr in self.trusted_proxy_cidrs:
                ip_network(cidr)
        except ValueError:
            raise ValueError("COURSE_TRUSTED_PROXY_CIDRS must contain valid CIDR networks") from None

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
            "site_origin": self.site_origin,
            "trusted_proxy_cidrs": self.trusted_proxy_cidrs,
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

    environment = values.get("COURSE_ENVIRONMENT", "development").strip().lower() or "development"
    if environment == "production" and not values.get("COURSE_SITE_ORIGIN", "").strip():
        raise ValueError("COURSE_SITE_ORIGIN must be an explicit HTTPS origin in production")
    site_origin = values.get("COURSE_SITE_ORIGIN", "http://127.0.0.1:8000").strip()
    raw_proxies = values.get("COURSE_TRUSTED_PROXY_CIDRS", "").strip()
    trusted_proxy_cidrs = tuple(part.strip() for part in raw_proxies.split(",")) if raw_proxies else ()

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
        environment=environment,
        site_origin=site_origin,
        trusted_proxy_cidrs=trusted_proxy_cidrs,
    )
