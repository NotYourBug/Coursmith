"""Small, typed audit payloads; success commits belong to the business caller."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .database import to_db_time, transaction, utc_now
from .domain import BusinessError


@dataclass(frozen=True)
class AuditEvent:
    actor_admin_id: int | None
    object_type: str
    object_id: str
    action: str
    reason: str
    outcome: Literal['success', 'denied']
    request_id: str
    changes: dict[str, object]


# No arbitrary titles, notes, credentials, hashes, HTML or request objects.
# Each new action must explicitly declare the metadata its consumer needs.
ACTION_FIELDS = {
    "security.rate_limit": {"error_code"},
    "security.csrf": {"error_code"},
    "auth.login": {"error_code"},
    "auth.logout": set(),
    "admin.password_change": {"revoked_sessions", "revision", "error_code"},
    "category.create": {"revision", "error_code"},
    "category.update": {"enabled", "sort_order", "revision", "error_code"},
    "product.create": {"status", "revision", "error_code"},
    "product.update": {"status", "access_days", "update_policy", "revision", "error_code"},
    "product.sales_check": {"check_ok", "revision", "error_code"},
    "code.issue": {"quantity", "access_days", "activation_days", "update_policy", "error_code"},
    "code.reissue": {"quantity", "revision", "error_code"},
    "code.void": {"revision", "error_code"},
    "code.redeem": {"error_code"},
    "order.create": {"amount_cents", "status", "revision", "error_code"},
    "order.update": {"amount_cents", "status", "revision", "error_code"},
    "order.refund": {"status", "revision", "revoked_sessions", "error_code"},
    "entitlement.revoke": {"revision", "revoked_sessions", "error_code"},
    "entitlement.extend": {"access_days", "revision", "error_code"},
    "recovery.issue": {"revision", "error_code"},
    "recovery.recover": {"revoked_sessions", "error_code"},
    "recovery.reset": {"revoked_sessions", "revision", "error_code"},
    "session.revoke": {"revoked_sessions", "error_code"},
    "legacy.verify": {"revision", "error_code"},
}
_SAFE_IDENTIFIER = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_CREDENTIAL = re.compile(r"(?:CS|LK)-[A-Za-z0-9_-]+")


def _invalid() -> BusinessError:
    return BusinessError("audit_payload", "Audit event contains unsupported or unsafe metadata.", 400)


def _validate(event: AuditEvent) -> str:
    if event.action not in ACTION_FIELDS or event.outcome not in ("success", "denied"):
        raise _invalid()
    if event.actor_admin_id is not None and (type(event.actor_admin_id) is not int or event.actor_admin_id < 1):
        raise _invalid()
    for value in (event.object_type, event.object_id, event.reason, event.request_id):
        if not isinstance(value, str) or not value or len(value) > 1000 or _CREDENTIAL.search(value):
            raise _invalid()
    if not _SAFE_IDENTIFIER.fullmatch(event.object_type):
        raise _invalid()
    if type(event.changes) is not dict or not event.changes.keys() <= ACTION_FIELDS[event.action]:
        raise _invalid()
    for key, value in event.changes.items():
        if key == "error_code":
            valid = isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value)
        elif key == "status":
            valid = value in ("draft", "active", "paused", "archived", "paid", "refunded")
        elif key == "update_policy":
            valid = value == "current_version"
        elif key in ("enabled", "check_ok"):
            valid = type(value) is bool
        elif key == "access_days":
            valid = value is None or (type(value) is int and 1 <= value <= 3650)
        elif key == "activation_days":
            valid = type(value) is int and 1 <= value <= 365
        elif key == "quantity":
            valid = type(value) is int and 1 <= value <= 200
        elif key == "sort_order":
            valid = type(value) is int
        else:
            valid = type(value) is int and value >= (1 if key == "revision" else 0)
        if not valid:
            raise _invalid()
    return json.dumps(event.changes, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def append_event(connection: sqlite3.Connection, event: AuditEvent) -> None:
    """Append without beginning, committing or rolling back the caller's work."""
    if not connection.in_transaction:
        raise BusinessError("audit_transaction", "Audit requires an active business transaction.", 409)
    changes_json = _validate(event)
    connection.execute(
        """INSERT INTO admin_events
           (actor_admin_id, object_type, object_id, action, reason, changes_json, outcome, request_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (event.actor_admin_id, event.object_type, event.object_id, event.action, event.reason,
         changes_json, event.outcome, event.request_id, to_db_time(utc_now())),
    )


def record_denial(db_path: Path, event: AuditEvent) -> None:
    """Call only after the failed business transaction has exited and unlocked."""
    if event.outcome != "denied":
        raise _invalid()
    _validate(event)
    with transaction(db_path, immediate=True) as connection:
        append_event(connection, event)
