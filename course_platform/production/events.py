"""Production denial events contain identifiers and codes, never source text."""

from __future__ import annotations

import re
from contextlib import closing
from pathlib import Path

from ..audit import AuditEvent, record_denial
from ..database import open_readonly
from ..domain import Actor, BusinessError


PRODUCTION_ACTIONS = frozenset({"production.create", "production.update", "production.reference",
                               "production.mode", "production.archive", "production.outline",
                               "production.confirm", "production.chapter", "production.candidate",
                               "production.acknowledge", "job.submit", "job.cancel", "job.interrupt"})
_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")


def record_production_denial(db_path: Path, actor: Actor | None, action: str, object_id: str,
                             error: BusinessError, *, request_id: str) -> None:
    """Persist only after rollback; routes may call again without duplication."""
    if getattr(error, "denial_recorded", False):
        return
    if action not in PRODUCTION_ACTIONS:
        raise BusinessError("audit_payload", "Unsupported production audit action.", 400)
    admin_id = None
    if isinstance(actor, Actor) and type(actor.admin_id) is int and 1 <= actor.admin_id <= 9223372036854775807:
        with closing(open_readonly(db_path)) as connection:
            if connection.execute("SELECT 1 FROM admins WHERE id=?", (actor.admin_id,)).fetchone():
                admin_id = actor.admin_id
    code = error.code if isinstance(error.code, str) and _CODE.fullmatch(error.code) else "production_error"
    # Route object IDs are untrusted before service validation.
    safe_object_id = object_id if isinstance(object_id, str) and (
        object_id in ("new", "unknown") or (object_id.isascii() and object_id.isdecimal() and len(object_id) <= 20)
    ) else "unknown"
    record_denial(db_path, AuditEvent(admin_id, "job" if action.startswith("job.") else "production", safe_object_id, action, code,
                                     "denied", request_id, {"error_code": code}))
    error.denial_recorded = True
