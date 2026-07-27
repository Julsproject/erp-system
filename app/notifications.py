"""Notifications Center — one inbox for the alerts that are otherwise scattered
across nav badges and the Dashboard's "Needs your attention" widget.

The alert conditions are *derived* from live data (low/out of stock, overdue &
due-soon credits, cheques due, pending deliveries, stale backup, below-cost
pricing). `sync_notifications` reconciles that live set into `Notification`
rows so the inbox gains read state and history without us storing an event
every time something changes:

  - a condition that newly holds  -> insert an unread row,
  - a row whose condition cleared -> mark resolved (kept as history),
  - a condition that recurs after resolving -> a fresh unread row.

The sweep is throttled (see `SWEEP_INTERVAL`) because it is triggered from the
nav badge, which renders on every admin page load.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from . import models, settings_store
from .backup import latest_backup
from .database import SessionLocal, get_db
from .deps import get_current_user, is_admin
from .templating import templates

router = APIRouter()

MANILA = ZoneInfo("Asia/Manila")
DUE_SOON_DAYS = 15
CHEQUE_SOON_DAYS = 3
BACKUP_STALE_DAYS = 2
SWEEP_INTERVAL = timedelta(minutes=2)   # don't recompute more often than this
ZERO = Decimal("0")

NO_INVOICE_WINDOW_DAYS = 30   # how long a no-invoice return stays in the Active list

CATEGORY_LABELS = {
    "stock": "Inventory", "pricing": "Pricing", "credit": "Credits", "payable": "Payables",
    "cheque": "Cheques", "delivery": "Deliveries", "backup": "Backup", "pos": "Point of Sale",
}


def _today() -> date:
    return datetime.now(MANILA).date()


def _now() -> datetime:
    return datetime.now(MANILA)


def _qty_expr():
    return models.Product.beginning_stock + models.Product.stock_qty


def _peso(value) -> str:
    try:
        return "₱{:,.2f}".format(Decimal(value or 0))
    except Exception:
        return "₱0.00"


def _current_alerts(db: Session) -> dict:
    """Compute the live alert set: dedupe_key -> alert fields."""
    today = _today()
    alerts: dict[str, dict] = {}

    # ---- stock -----------------------------------------------------------
    out = (
        db.query(models.Product)
        .filter(models.Product.is_active.is_(True), _qty_expr() <= 0)
        .all()
    )
    for p in out:
        alerts[f"stock_out:{p.id}"] = {
            "category": "stock", "severity": "danger",
            "title": f"Out of stock: {p.name}",
            "body": "On-hand quantity has reached zero. Reorder to keep selling it.",
            "link": "/products",
        }
    low = (
        db.query(models.Product)
        .filter(
            models.Product.is_active.is_(True),
            models.Product.reorder_level > 0,
            _qty_expr() > 0,
            _qty_expr() <= models.Product.reorder_level,
        )
        .all()
    )
    for p in low:
        alerts[f"stock_low:{p.id}"] = {
            "category": "stock", "severity": "warning",
            "title": f"Low stock: {p.name}",
            "body": f"On hand {p.total_qty} is at or below the reorder level of {p.reorder_level}.",
            "link": "/products",
        }

    # ---- below-cost pricing ---------------------------------------------
    below = (
        db.query(models.Product)
        .filter(
            models.Product.is_active.is_(True),
            models.Product.cost_price > 0,
            models.Product.selling_price <= models.Product.cost_price,
        )
        .all()
    )
    for p in below:
        alerts[f"price_below_cost:{p.id}"] = {
            "category": "pricing", "severity": "danger",
            "title": f"Selling at or below cost: {p.name}",
            "body": f"Selling price {_peso(p.selling_price)} is not above cost {_peso(p.cost_price)} — every sale loses money.",
            "link": "/products?alert=1",
        }

    # ---- below the shop's minimum-margin target (softer than below-cost) --
    # Only when the owner has actually set a target (see Settings). A product
    # already flagged above (at/below cost) isn't flagged again here — that's
    # the harder error; this is "profitable, but thinner than your standard."
    min_margin = settings_store.min_margin_pct()
    if min_margin is not None:
        priced = (
            db.query(models.Product)
            .filter(
                models.Product.is_active.is_(True),
                models.Product.cost_price > 0,
                models.Product.selling_price > models.Product.cost_price,
            )
            .all()
        )
        for p in priced:
            price = Decimal(str(p.selling_price or 0))
            cost = Decimal(str(p.cost_price or 0))
            margin_pct = float((price - cost) / price * 100) if price > 0 else 0.0
            if margin_pct < min_margin:
                alerts[f"thin_margin:{p.id}"] = {
                    "category": "pricing", "severity": "warning",
                    "title": f"Below {min_margin:g}% margin target: {p.name}",
                    "body": f"Selling {_peso(price)} on a {_peso(cost)} cost is only {margin_pct:.1f}% margin, "
                            f"under your {min_margin:g}% target — still profitable, just thinner than your standard.",
                    "link": f"/products/{p.id}/edit",
                }

    # ---- credits (overdue + due soon) -----------------------------------
    settled_sub = (
        db.query(
            models.ReceivableSettlement.sale_id.label("sid"),
            func.coalesce(func.sum(models.ReceivableSettlement.amount), 0).label("paid"),
        )
        .group_by(models.ReceivableSettlement.sale_id)
        .subquery()
    )
    credit_rows = (
        db.query(models.Sale, func.coalesce(settled_sub.c.paid, 0))
        .outerjoin(settled_sub, settled_sub.c.sid == models.Sale.id)
        .filter(models.Sale.receivable_amount > 0)
        .all()
    )
    horizon = today + timedelta(days=DUE_SOON_DAYS)
    # A balance waiting on a COD delivery isn't late credit — the driver collects
    # it on handover — so it must not raise an "overdue credit" alert.
    from .deliveries import cod_pending_sale_ids
    cod_ids = cod_pending_sale_ids(db)
    for sale, paid in credit_rows:
        outstanding = Decimal(str(sale.receivable_amount or 0)) - Decimal(str(paid or 0))
        if outstanding <= 0 or not sale.due_date or sale.id in cod_ids:
            continue
        who = sale.customer_name or "a customer"
        if sale.due_date < today:
            days = (today - sale.due_date).days
            alerts[f"credit_overdue:{sale.id}"] = {
                "category": "credit", "severity": "danger",
                "title": f"Overdue credit: {who}",
                "body": f"Invoice {sale.invoice_no} — {_peso(outstanding)} outstanding, {days} day(s) past due.",
                "link": "/sales/receivables",
            }
        elif sale.due_date <= horizon:
            days = (sale.due_date - today).days
            alerts[f"credit_due:{sale.id}"] = {
                "category": "credit", "severity": "warning",
                "title": f"Credit due soon: {who}",
                "body": f"Invoice {sale.invoice_no} — {_peso(outstanding)} due in {days} day(s).",
                "link": "/sales/receivables",
            }

    # ---- payables (overdue + due soon) ------------------------------------
    # A 'confirmed' receive-type purchase IS the payable — goods/cost already
    # moved, payment hasn't. Mirrors the credit alerts above.
    payables = (
        db.query(models.Purchase)
        .filter(
            models.Purchase.txn_type == "receive", models.Purchase.status == "confirmed",
            models.Purchase.due_date.isnot(None),
        )
        .all()
    )
    for p in payables:
        who = p.supplier.name if p.supplier else "a supplier"
        if p.due_date < today:
            days = (today - p.due_date).days
            alerts[f"payable_overdue:{p.id}"] = {
                "category": "payable", "severity": "danger",
                "title": f"Overdue payable: {who}",
                "body": f"{p.ref_no} — {_peso(p.total)} outstanding, {days} day(s) past due.",
                "link": "/purchases/payables",
            }
        elif p.due_date <= horizon:
            days = (p.due_date - today).days
            alerts[f"payable_due:{p.id}"] = {
                "category": "payable", "severity": "warning",
                "title": f"Payable due soon: {who}",
                "body": f"{p.ref_no} — {_peso(p.total)} due in {days} day(s).",
                "link": "/purchases/payables",
            }

    # ---- cheques due / overdue ------------------------------------------
    cheques = (
        db.query(models.PostDatedCheque)
        .filter(
            models.PostDatedCheque.status == "pending",
            models.PostDatedCheque.cheque_date <= today + timedelta(days=CHEQUE_SOON_DAYS),
        )
        .all()
    )
    for c in cheques:
        overdue = c.cheque_date < today
        direction = "Received" if c.direction == "received" else "Issued"
        alerts[f"cheque_due:{c.id}"] = {
            "category": "cheque", "severity": "danger" if overdue else "warning",
            "title": f"Cheque {'overdue' if overdue else 'due soon'}: {direction.lower()} {_peso(c.amount)}",
            "body": f"{direction} cheque {c.cheque_no or ''} dated {c.cheque_date.strftime('%b %d, %Y')}"
                    f"{' — past due, clear or update it.' if overdue else '.'}".strip(),
            "link": "/pdc",
        }

    # ---- no-invoice refunds/exchanges -------------------------------------
    # A refund/exchange where the cashier couldn't match an invoice means the
    # returned item's price came from what was typed in, not a verified sale
    # line — worth a quick spot-check. Windowed to NO_INVOICE_WINDOW_DAYS so
    # these age out of the Active list on their own rather than accumulating
    # forever; they remain visible under History once resolved.
    no_invoice_sales = (
        db.query(models.Sale)
        .filter(
            models.Sale.no_invoice_return.is_(True),
            models.Sale.created_at >= _now() - timedelta(days=NO_INVOICE_WINDOW_DAYS),
        )
        .all()
    )
    for s in no_invoice_sales:
        kind = "Refund" if s.txn_type == "refund" else "Exchange"
        amount = s.change_amount if s.txn_type == "refund" else abs(s.total or 0)
        alerts[f"no_invoice_return:{s.id}"] = {
            "category": "pos", "severity": "warning",
            "title": f"No-invoice {kind.lower()}: {s.invoice_no}",
            "body": f"{kind} of {_peso(amount)} for {s.customer_name or 'a walk-in customer'} was processed "
                    "without matching an original invoice — the item and price came from the cashier, worth a spot-check.",
            "link": f"/pos/receipt/{s.id}?from=sales",
        }

    # ---- backup health (singleton) --------------------------------------
    lb = latest_backup()
    if not lb:
        alerts["backup_missing"] = {
            "category": "backup", "severity": "danger",
            "title": "No database backup found",
            "body": "There is no backup on record. Create one so a day's sales can never be lost.",
            "link": "/backup",
        }
    else:
        age = (today - lb["when"].date()).days
        if age > BACKUP_STALE_DAYS:
            alerts["backup_stale"] = {
                "category": "backup", "severity": "warning",
                "title": "Backup is out of date",
                "body": f"The newest backup is {age} day(s) old. Make a fresh backup.",
                "link": "/backup",
            }

    return alerts


def sync_notifications(db: Session) -> None:
    """Reconcile the live alert set into Notification rows (see module docstring)."""
    current = _current_alerts(db)
    existing = {
        n.dedupe_key: n
        for n in db.query(models.Notification).filter(models.Notification.is_resolved.is_(False)).all()
    }
    now = _now()

    # Resolve rows whose condition no longer holds — they become history.
    for key, n in existing.items():
        if key not in current:
            n.is_resolved = True
            n.resolved_at = now

    # Insert new conditions; refresh the text of ones still open.
    for key, data in current.items():
        n = existing.get(key)
        if n is None or n.is_resolved:
            db.add(models.Notification(dedupe_key=key, is_read=False, is_resolved=False, **data))
        else:
            n.category = data["category"]
            n.severity = data["severity"]
            n.title = data["title"]
            n.body = data["body"]
            n.link = data["link"]

    db.commit()


def _maybe_sweep(db: Session) -> None:
    """Run the sweep at most once per SWEEP_INTERVAL, tracked in app_settings."""
    last_raw = settings_store.get_setting(db, "notif_last_sweep", "")
    try:
        last = datetime.fromisoformat(last_raw) if last_raw else None
    except ValueError:
        last = None
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=MANILA)
    if last is None or (_now() - last) >= SWEEP_INTERVAL:
        sync_notifications(db)
        settings_store.set_setting(db, "notif_last_sweep", _now().isoformat())
        db.commit()


def unread_count() -> int:
    """Live unread count for the sidebar badge. Runs a throttled sweep first so
    the badge stays fresh, then counts. Never raises — a badge must not break a
    page render.
    """
    db = SessionLocal()
    try:
        _maybe_sweep(db)
        return (
            db.query(func.count(models.Notification.id))
            .filter(models.Notification.is_read.is_(False), models.Notification.is_resolved.is_(False))
            .scalar()
        ) or 0
    except Exception:
        return 0
    finally:
        db.close()


@router.get("/notifications", response_class=HTMLResponse)
def list_notifications(
    request: Request,
    view: str = "active",
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)

    sync_notifications(db)
    settings_store.set_setting(db, "notif_last_sweep", _now().isoformat())
    db.commit()

    view = "history" if view == "history" else "active"
    sev_rank = case(
        (models.Notification.severity == "danger", 0),
        (models.Notification.severity == "warning", 1),
        else_=2,
    )
    base = db.query(models.Notification)
    if view == "history":
        rows = (
            base.filter(models.Notification.is_resolved.is_(True))
            .order_by(models.Notification.resolved_at.desc().nullslast(), models.Notification.id.desc())
            .limit(200)
            .all()
        )
    else:
        rows = (
            base.filter(models.Notification.is_resolved.is_(False))
            .order_by(models.Notification.is_read, sev_rank, models.Notification.created_at.desc())
            .all()
        )

    active_count = (
        db.query(func.count(models.Notification.id))
        .filter(models.Notification.is_resolved.is_(False))
        .scalar()
    ) or 0
    unread = (
        db.query(func.count(models.Notification.id))
        .filter(models.Notification.is_read.is_(False), models.Notification.is_resolved.is_(False))
        .scalar()
    ) or 0

    return templates.TemplateResponse(
        "notifications/list.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "notifications": rows, "view": view, "today": _today(),
            "active_count": active_count, "unread": unread,
            "category_labels": CATEGORY_LABELS,
        },
    )


@router.post("/notifications/{notif_id:int}/read")
def mark_read(notif_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    n = db.get(models.Notification, notif_id)
    if n and not n.is_read:
        n.is_read = True
        n.read_at = _now()
        db.commit()
    return RedirectResponse("/notifications", status_code=status.HTTP_302_FOUND)


@router.post("/notifications/read-all")
def mark_all_read(db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    db.query(models.Notification).filter(
        models.Notification.is_read.is_(False), models.Notification.is_resolved.is_(False)
    ).update({"is_read": True, "read_at": _now()}, synchronize_session=False)
    db.commit()
    return RedirectResponse("/notifications", status_code=status.HTTP_302_FOUND)
