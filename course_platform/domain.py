"""Shared identities, clocks and safe errors for owner operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Actor:
    """Trusted identity supplied by the admin session or owner CLI."""

    admin_id: int
    request_id: str


class BusinessError(Exception):
    """An error safe to show to a caller; never carry SQL or credentials."""

    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
