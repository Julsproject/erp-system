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

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from . import models
from .database import get_db
from .deps import get_current_user, is_admin, safe_back_url, url_with
from .templating import templates

router = APIRouter()

PAGE_SIZE = 15

# For the viewer's filter dropdowns — label the codes we actually emit.
ACTION_LABELS = {
    "create": "Created", "update": "Edited", "archive": "Archived",
    "void": "Voided", "cancel": "Cancelled", "confirm": "Confirmed",
    "pay": "Marked paid", "dispatch": "Dispatched", "complete": "Completed",
    "adjust_stock": "Stock adjusted", "stock_count": "Stock count correction", "convert": "Converted",
    "login": "Signed in", "login_failed": "Failed sign-in", "logout": "Signed out",
    "password_change": "Password changed", "settings_change": "Settings changed",
    "merge": "Merged", "unmerge": "Unmerged",
}
ENTITY_LABELS = {
    "product": "Inventory", "expense": "Expense", "delivery": "Delivery",
    "customer": "Customer", "supplier": "Supplier", "user": "User",
    "bank_account": "Bank account", "bank_transaction": "Bank transaction",
    "purchase": "Purchase", "quotation": "Quotation", "pdc": "Cheque",
    "setting": "Settings", "auth": "Sign-in", "shift": "Cash Drawer",
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


# Merges the Activity Log can undo, and the plain-word name for the message.
UNMERGE_TYPES = {"product": "product", "customer": "customer"}


def merge_undo_data(log: models.AuditLog):
    """The undo record a merge saved (under "_unmerge" in its changes), or
    None for merges logged before the Unmerge button existed — those didn't
    note which rows moved, so there's nothing reliable to reverse."""
    if not log or log.action != "merge" or log.entity_type not in UNMERGE_TYPES or not log.changes:
        return None
    try:
        return json.loads(log.changes).get("_unmerge")
    except (ValueError, AttributeError):
        return None


def unmerged_log_ids(db: Session) -> set:
    """Merge log ids that an Unmerge has already reversed."""
    done = set()
    for (changes,) in db.query(models.AuditLog.changes).filter(
        models.AuditLog.action == "unmerge", models.AuditLog.changes.isnot(None)
    ):
        try:
            rid = json.loads(changes).get("_reverses")
        except (ValueError, AttributeError):
            continue
        if rid:
            done.add(int(rid))
    return done


@router.post("/audit/{log_id:int}/unmerge")
def unmerge(log_id: int, request: Request, back: str = Form(""),
            db: Session = Depends(get_db), user=Depends(get_current_user)):
    """The Unmerge button on a merge entry — hands off to the product or
    customer module, which knows what that merge moved."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    from .customers import unmerge_customer
    from .products import unmerge_product

    # Back to the log as it was filtered, with only this outcome's notice on it.
    back = safe_back_url(back, "/audit")

    def _back(key, msg):
        notice = {"ok": None, "error": None, key: msg}
        return RedirectResponse(url_with(back, **notice), status_code=302)

    log = db.get(models.AuditLog, log_id)
    undo = merge_undo_data(log)
    if undo is None:
        return _back("error", "This merge was done before Unmerge existed, so it didn't record what moved — it can't be undone automatically.")
    if log_id in unmerged_log_ids(db):
        return _back("error", "That merge has already been undone.")
    handler = unmerge_product if log.entity_type == "product" else unmerge_customer
    try:
        msg = handler(db, log, undo, user=user, request=request)
    except ValueError as e:
        db.rollback()
        return _back("error", str(e))
    db.commit()
    return _back("ok", msg)


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
    ok: str = "",
    error: str = "",
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

    # Decode the JSON change maps for display. Keys starting with "_" are
    # bookkeeping (e.g. a merge's undo record), not field edits to show.
    reversed_merges = unmerged_log_ids(db) if any(r.action == "merge" for r in rows) else set()
    entries = []
    for r in rows:
        changes = None
        if r.changes:
            try:
                changes = {k: v for k, v in json.loads(r.changes).items() if not k.startswith("_")} or None
            except (ValueError, AttributeError):
                changes = None
        unmerge = None
        if r.action == "merge" and r.entity_type in UNMERGE_TYPES:
            if r.id in reversed_merges:
                unmerge = "done"
            elif merge_undo_data(r) is not None:
                unmerge = "ready"
            else:
                unmerge = "old"
        entries.append({"row": r, "changes": changes, "unmerge": unmerge})

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
            "ok": ok, "error": error,
        },
    )
