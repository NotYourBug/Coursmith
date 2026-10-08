"""Owner overview and bounded, minimal audit projections."""
from contextlib import closing
from datetime import datetime
import re

from fastapi import APIRouter, Request

from ...database import open_readonly, to_db_time
from ...domain import BusinessError
from ...operations.orders import display_time
from .auth import AdminRoute, CSRF_COOKIE, _render, require_owner

router = APIRouter(prefix="/admin", route_class=AdminRoute)


def audit_rows(request, *, recent=False):
    filters, values = [], []
    query = request.query_params
    for key, column in (("actor", "actor_admin_id"), ("action", "action"), ("object", "object_id")):
        value = query.get(key, "") if not recent else ""
        if value:
            if len(value) > 100 or not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
                raise BusinessError("invalid_filter", "Invalid audit filter.", 400)
            filters.append(column + "=?")
            values.append(value)
    for key, operator in (("from", ">="), ("to", "<=")):
        value = query.get(key, "") if not recent else ""
        if value:
            try:
                instant = datetime.fromisoformat(value)
                if instant.tzinfo is None:
                    raise ValueError
            except ValueError:
                raise BusinessError("invalid_filter", "Time filter requires a timezone.", 400) from None
            filters.append("created_at" + operator + "?")
            values.append(to_db_time(instant))
    page_value = query.get("page", "1") if not recent else "1"
    if not re.fullmatch(r"[1-9][0-9]{0,8}", page_value):
        raise BusinessError("invalid_page", "Invalid page.", 400)
    page = int(page_value)
    where = " WHERE " + " AND ".join(filters) if filters else ""
    with closing(open_readonly(request.app.state.settings.database_path)) as connection:
        total = connection.execute("SELECT count(*) FROM admin_events" + where, values).fetchone()[0]
        rows = [dict(row) for row in connection.execute("""SELECT actor_admin_id, action, object_type,
            object_id, outcome, created_at FROM admin_events""" + where + " ORDER BY id DESC LIMIT 20 OFFSET ?",
            (*values, (page-1)*20))]
    for row in rows:
        row["created_at"] = display_time(row["created_at"])
    return rows, total, page


@router.get("")
@router.get("/")
def dashboard(request: Request):
    owner = require_owner(request)
    with closing(open_readonly(request.app.state.settings.database_path)) as connection:
        counts = {
            "drafts": connection.execute("SELECT count(*) FROM products WHERE status='draft'").fetchone()[0],
            "verification": connection.execute("SELECT count(*) FROM entitlements WHERE legacy_state='pending_verification'").fetchone()[0],
            "codes": connection.execute("SELECT count(*) FROM access_codes WHERE legacy_state='pending_verification'").fetchone()[0],
            "delivery": connection.execute("SELECT count(*) FROM orders WHERE status='paid' AND delivery_state IN ('recorded','code_ready')").fetchone()[0],
            "activated": connection.execute("SELECT count(*) FROM orders WHERE delivery_state='activated'").fetchone()[0],
        }
    return _render(request, "dashboard.html", authenticated=True, revision=owner.revision,
        csrf_token=request.cookies.get(CSRF_COOKIE, ""), counts=counts, events=audit_rows(request, recent=True)[0])


@router.get("/audit")
def audit(request: Request):
    owner = require_owner(request)
    rows, total, page = audit_rows(request)
    return _render(request, "audit.html", authenticated=True, revision=owner.revision,
        csrf_token=request.cookies.get(CSRF_COOKIE, ""), events=rows, total=total, page=page)
