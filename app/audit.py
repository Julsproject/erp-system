"""Audit trail helper: record who did what, and view it.

Call `record(...)` from a route right before (or after) it mutates something;
the row is added to the *same* session, so it commits atomically with the
change it describes — the log can never disagree with what actually happened.

Snapshot fields with `snapshot(obj, FIELDS)` before and after an edit, then pass
`changes=diff(before, after)` to record only the fields that actually changed,
as a `{field: [old, new]}` map.
"""
import json
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from . import models
from .database import get_db
from .deps import get_current_user, is_admin
from .templating import templates

router = APIRouter()

PAGE_SIZE = 40

# For the viewer's filter dropdowns — label the codes we actually emit.
ACTION_LABELS = {
    "create": "Created", "update": "Edited", "archive": "Archived",
    "void": "Voided", "cancel": "Cancelled", "confirm": "Confirmed",
    "pay": "Marked paid", "dispatch": "Dispatched", "complete": "Completed",
    "adjust_stock": "Stock adjusted", "convert": "Converted",
    "login": "Signed in", "login_failed": "Failed sign-in", "logout": "Signed out",
    "password_change": "Password changed", "settings_change": "Settings changed",
}
ENTITY_LABELS = {
    "product": "Inventory", "expense": "Expense", "delivery": "Delivery",
    "customer": "Customer", "supplier": "Supplier", "user": "User",
    "bank_account": "Bank account", "bank_transaction": "Bank transaction",
    "purchase": "Purchase", "quotation": "Quotation", "pdc": "Cheque",
    "setting": "Settings", "auth": "Sign-in",
}


def _plain(value):
    """Make a value JSON-safe and comparable (Decimal/date -> str)."""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return value


def snapshot(obj, fields) -> dict:
    """{field: plain-value} for the given attributes of an ORM object."""
    return {f: _plain(getattr(obj, f, None)) for f in fields}


def diff(before: dict, after: dict) -> dict:
    """{field: [old, new]} for fields whose value changed."""
    out = {}
    for f in after:
        if before.get(f) != after.get(f):
            out[f] = [before.get(f), after.get(f)]
    return out


def client_ip(request: Request) -> str | None:
    if request is None:
        return None
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()[:45]
    return request.client.host if request.client else None


def record(
    db: Session,
    *,
    action: str,
    entity_type: str,
    user=None,
    request: Request = None,
    entity_id: int = None,
    entity_label: str = None,
    summary: str = None,
    changes: dict = None,
) -> None:
    """Add an audit row to the current session (caller commits)."""
    db.add(models.AuditLog(
        user_id=(user.id if user else None),
        username=(user.username if user else None),
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_label=(entity_label or "")[:150] or None,
        summary=(summary or "")[:300] or None,
        changes=(json.dumps(changes, ensure_ascii=False, default=str) if changes else None),
        ip=client_ip(request),
    ))


@router.get("/audit", response_class=HTMLResponse)
def audit_log(
    request: Request,
    q: str = "",
    action: str = "",
    entity_type: str = "",
    user_id: int = 0,
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)

    q = (q or "").strip()
    page = max(page, 1)

    def _parse(s):
        try:
            return date.fromisoformat(s) if s else None
        except ValueError:
            return None

    query = db.query(models.AuditLog)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            models.AuditLog.summary.ilike(like),
            models.AuditLog.entity_label.ilike(like),
            models.AuditLog.username.ilike(like),
        ))
    if action:
        query = query.filter(models.AuditLog.action == action)
    if entity_type:
        query = query.filter(models.AuditLog.entity_type == entity_type)
    if user_id:
        query = query.filter(models.AuditLog.user_id == user_id)
    df, dt = _parse(date_from), _parse(date_to)
    if df:
        query = query.filter(models.AuditLog.created_at >= datetime.combine(df, datetime.min.time()))
    if dt:
        query = query.filter(models.AuditLog.created_at <= datetime.combine(dt, datetime.max.time()))

    total = query.count()
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, pages)
    rows = (
        query.order_by(models.AuditLog.id.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
        .all()
    )

    # Decode the JSON change maps for display.
    entries = []
    for r in rows:
        changes = None
        if r.changes:
            try:
                changes = json.loads(r.changes)
            except ValueError:
                changes = None
        entries.append({"row": r, "changes": changes})

    users = db.query(models.User).order_by(models.User.username).all()

    return templates.TemplateResponse(
        "audit/list.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "entries": entries, "users": users,
            "action_labels": ACTION_LABELS, "entity_labels": ENTITY_LABELS,
            "q": q, "action": action, "entity_type": entity_type, "user_id": user_id,
            "date_from": date_from, "date_to": date_to,
            "page": page, "pages": pages, "total": total,
        },
    )
