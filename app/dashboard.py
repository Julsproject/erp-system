"""Dashboard: daily numbers, charts, alerts and product performance.

All date grouping is done in Manila time so "today" means the store's day, not
the server's UTC day.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from . import models
from .backup import latest_backup
from .database import get_db
from .deps import get_current_user
from .templating import templates

router = APIRouter()

MANILA = ZoneInfo("Asia/Manila")
DUE_SOON_DAYS = 15
BACKUP_STALE_DAYS = 2   # warn when the newest backup is older than this
ZERO = Decimal("0")


def _today() -> date:
    return datetime.now(MANILA).date()


def _local_date(col):
    """The Manila calendar date of a timestamptz column."""
    return func.date(func.timezone("Asia/Manila", col))


def _qty_expr():
    return models.Product.beginning_stock + models.Product.stock_qty


def _sales_between(db: Session, start: date, end: date) -> Decimal:
    """Net sales (sales minus refunds, plus exchange differences)."""
    total = (
        db.query(func.coalesce(func.sum(models.Sale.total), 0))
        .filter(_local_date(models.Sale.created_at).between(start, end))
        .scalar()
    )
    return Decimal(str(total or 0))


def _profit_between(db: Session, start: date, end: date) -> Decimal:
    """Revenue minus cost of goods, using the cost frozen on each sale line."""
    cogs_expr = models.SaleLine.qty * models.SaleLine.unit_factor * models.SaleLine.unit_cost
    value = (
        db.query(func.coalesce(func.sum(models.SaleLine.line_total - cogs_expr), 0))
        .join(models.Sale, models.SaleLine.sale_id == models.Sale.id)
        .filter(
            models.Sale.txn_type == "sale",
            _local_date(models.Sale.created_at).between(start, end),
        )
        .scalar()
    )
    return Decimal(str(value or 0))


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, days: int = 7, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)

    if days not in (7, 30, 90):
        days = 7
    today = _today()
    period_start = today - timedelta(days=days - 1)
    month_start = today.replace(day=1)

    # ---- headline numbers ------------------------------------------------
    kpi = {
        "today": _sales_between(db, today, today),
        "month": _sales_between(db, month_start, today),
        "period": _sales_between(db, period_start, today),
        "profit": _profit_between(db, period_start, today),
    }

    inv_cost, inv_retail, sku_count = (
        db.query(
            func.coalesce(func.sum(_qty_expr() * models.Product.cost_price), 0),
            func.coalesce(func.sum(_qty_expr() * models.Product.selling_price), 0),
            func.count(models.Product.id),
        )
        .filter(models.Product.is_active.is_(True))
        .one()
    )
    inventory = {
        "cost": Decimal(str(inv_cost or 0)),
        "retail": Decimal(str(inv_retail or 0)),
        "skus": sku_count or 0,
    }

    # ---- sales trend (one bar per day, zero-filled) ----------------------
    rows = (
        db.query(
            _local_date(models.Sale.created_at).label("d"),
            func.coalesce(func.sum(models.Sale.total), 0),
        )
        .filter(_local_date(models.Sale.created_at).between(period_start, today))
        .group_by("d")
        .all()
    )
    by_day = {r[0]: Decimal(str(r[1] or 0)) for r in rows}
    trend = []
    for i in range(days):
        d = period_start + timedelta(days=i)
        trend.append({"date": d, "label": d.strftime("%b %d"), "short": d.strftime("%a"), "total": by_day.get(d, ZERO)})
    trend_max = max([t["total"] for t in trend] + [ZERO])
    for t in trend:
        t["pct"] = float(t["total"] / trend_max * 100) if trend_max > 0 else 0.0
        t["is_today"] = t["date"] == today

    # ---- payment method mix ---------------------------------------------
    pay_rows = (
        db.query(models.Payment.method, func.coalesce(func.sum(models.Payment.amount), 0))
        .join(models.Sale, models.Payment.sale_id == models.Sale.id)
        .filter(_local_date(models.Sale.created_at).between(period_start, today))
        .group_by(models.Payment.method)
        .all()
    )
    labels = {"cash": "Cash", "gcash": "GCash", "card": "Card", "bank_transfer": "Bank Transfer", "receivable": "Receivable"}
    colors = {"cash": "#16a34a", "gcash": "#2563eb", "card": "#7c3aed", "bank_transfer": "#0891b2", "receivable": "#d97706"}
    pay_total = sum((Decimal(str(a or 0)) for _, a in pay_rows), ZERO)
    payments = []
    for method, amount in sorted(pay_rows, key=lambda r: float(r[1] or 0), reverse=True):
        amt = Decimal(str(amount or 0))
        payments.append({
            "label": labels.get(method, method.title()),
            "color": colors.get(method, "#64748b"),
            "amount": amt,
            "pct": float(amt / pay_total * 100) if pay_total > 0 else 0.0,
        })

    # ---- stock alerts ----------------------------------------------------
    out_of_stock = (
        db.query(models.Product)
        .filter(models.Product.is_active.is_(True), _qty_expr() <= 0)
        .order_by(models.Product.name)
        .all()
    )
    low_stock = (
        db.query(models.Product)
        .filter(
            models.Product.is_active.is_(True),
            models.Product.reorder_level > 0,
            _qty_expr() > 0,
            _qty_expr() <= models.Product.reorder_level,
        )
        .order_by(models.Product.name)
        .all()
    )
    no_cost = (
        db.query(func.count(models.Product.id))
        .filter(models.Product.is_active.is_(True), models.Product.cost_price <= 0)
        .scalar()
    ) or 0

    # ---- credit / utang due --------------------------------------------
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
        .order_by(case((models.Sale.due_date.is_(None), 1), else_=0), models.Sale.due_date)
        .all()
    )
    due_soon, overdue = [], []
    utang_total = ZERO
    horizon = today + timedelta(days=DUE_SOON_DAYS)
    for sale, paid in credit_rows:
        outstanding = Decimal(str(sale.receivable_amount or 0)) - Decimal(str(paid or 0))
        if outstanding <= 0:
            continue
        utang_total += outstanding
        if not sale.due_date:
            continue
        entry = {"sale": sale, "outstanding": outstanding, "days": (sale.due_date - today).days}
        if sale.due_date < today:
            overdue.append(entry)
        elif sale.due_date <= horizon:
            due_soon.append(entry)

    # ---- product performance --------------------------------------------
    perf = (
        db.query(
            models.SaleLine.product_id,
            models.SaleLine.product_name,
            func.coalesce(func.sum(models.SaleLine.qty), 0).label("qty"),
            func.coalesce(func.sum(models.SaleLine.line_total), 0).label("revenue"),
        )
        .join(models.Sale, models.SaleLine.sale_id == models.Sale.id)
        .filter(
            models.Sale.txn_type == "sale",
            _local_date(models.Sale.created_at).between(period_start, today),
        )
        .group_by(models.SaleLine.product_id, models.SaleLine.product_name)
        .all()
    )
    top_revenue = sorted(perf, key=lambda r: float(r.revenue or 0), reverse=True)[:5]

    # Which customers bought the most in this period (walk-ins have no name).
    top_customers = (
        db.query(
            models.Sale.customer_name,
            func.count(models.Sale.id).label("orders"),
            func.coalesce(func.sum(models.Sale.total), 0).label("spent"),
        )
        .filter(
            models.Sale.txn_type == "sale",
            _local_date(models.Sale.created_at).between(period_start, today),
            models.Sale.customer_name.isnot(None),
            models.Sale.customer_name != "",
        )
        .group_by(models.Sale.customer_name)
        .order_by(func.coalesce(func.sum(models.Sale.total), 0).desc())
        .limit(5)
        .all()
    )

    # Dead stock: on hand but nothing sold in the last 30 days.
    sold_ids = {
        r[0] for r in db.query(models.SaleLine.product_id)
        .join(models.Sale, models.SaleLine.sale_id == models.Sale.id)
        .filter(_local_date(models.Sale.created_at) >= today - timedelta(days=30))
        .distinct()
        .all()
        if r[0]
    }
    dead_q = db.query(models.Product).filter(models.Product.is_active.is_(True), _qty_expr() > 0)
    if sold_ids:
        dead_q = dead_q.filter(~models.Product.id.in_(sold_ids))
    dead_stock = dead_q.order_by(models.Product.name).limit(8).all()

    recent = db.query(models.Sale).order_by(models.Sale.id.desc()).limit(10).all()

    # ---- backup health ---------------------------------------------------
    lb = latest_backup()
    if lb:
        age = (today - lb["when"].date()).days
        backup_info = {"last": lb["when"], "days": age, "missing": False, "stale": age > BACKUP_STALE_DAYS}
    else:
        backup_info = {"last": None, "days": None, "missing": True, "stale": True}

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "days": days, "today": today,
            "kpi": kpi, "inventory": inventory,
            "trend": trend, "trend_max": trend_max,
            "payments": payments, "pay_total": pay_total,
            "out_of_stock": out_of_stock, "low_stock": low_stock, "no_cost": no_cost,
            "utang_total": utang_total, "due_soon": due_soon, "overdue": overdue,
            "top_revenue": top_revenue, "top_customers": top_customers, "dead_stock": dead_stock,
            "recent": recent, "backup": backup_info, "backup_stale_days": BACKUP_STALE_DAYS,
        },
    )
