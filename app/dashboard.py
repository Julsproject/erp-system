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

from . import models, settings_store
from .backup import latest_backup
from .database import get_db
from .deps import get_current_user, is_staff
from .products import low_stock_expr
from .purchases import _purchase_outstanding, _settled_for_purchases
from .templating import templates

router = APIRouter()

MANILA = ZoneInfo("Asia/Manila")
DUE_SOON_DAYS = 15
BACKUP_STALE_DAYS = 2   # warn when the newest backup is older than this
MAX_TREND_LABELS = 8    # cap on x-axis date labels shown under the sales trend
ZERO = Decimal("0")

# Aging buckets shared by receivables and payables on the dashboard. Coarser
# than the dedicated /purchases/payables/aging page (which splits out to
# 90+ days) — this is a rollup meant to be read in a glance, not a statement.
AGING_BUCKETS = ["current", "1_30", "31_60", "over_60"]
AGING_LABELS = {"current": "Current", "1_30": "1–30 days", "31_60": "31–60 days", "over_60": "60+ days"}


def _aging_bucket(due_date, today: date) -> str:
    if not due_date or due_date >= today:
        return "current"
    overdue_days = (today - due_date).days
    if overdue_days <= 30:
        return "1_30"
    if overdue_days <= 60:
        return "31_60"
    return "over_60"


def _aging_rows(bucket_totals: dict, total: Decimal):
    """Bucket totals -> display rows (label, amount, share of the bar), in
    bucket order, so the template just loops without doing arithmetic."""
    return [
        {
            "label": AGING_LABELS[b],
            "amount": bucket_totals[b],
            "pct": float(bucket_totals[b] / total * 100) if total > 0 else 0.0,
            "overdue": b != "current",
        }
        for b in AGING_BUCKETS
    ]


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
        .filter(
            _local_date(models.Sale.created_at).between(start, end),
            models.Sale.is_voided.is_(False),
        )
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
            models.Sale.is_voided.is_(False),
            _local_date(models.Sale.created_at).between(start, end),
        )
        .scalar()
    )
    return Decimal(str(value or 0))


def _parse_date(s: str):
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def _pct_change(current: Decimal, previous: Decimal):
    """Percent change vs a previous value, but only when the base is positive.

    Returns None when previous <= 0: a zero base has no meaningful %, and a
    *negative* base (e.g. last period was a loss) makes the sign flip —
    recovering from −40 to +60 would read as "−250%". In those cases the
    template shows an arrow based on the raw values instead of a bogus %."""
    prev = Decimal(str(previous or 0))
    if prev <= 0:
        return None
    return float((Decimal(str(current or 0)) - prev) / prev * 100)


CREDIT_ALERT_PAGE_SIZE = 10


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    days: int = 7,
    date_from: str = "",
    date_to: str = "",
    credit_page: int = 1,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)

    today = _today()

    # A custom range (both ends given) overrides the 7/30/90 presets.
    df, dt = _parse_date(date_from), _parse_date(date_to)
    custom = bool(df and dt)
    if custom:
        if dt > today:
            dt = today
        if df > dt:
            df, dt = dt, df
        if (dt - df).days > 365:
            df = dt - timedelta(days=365)
        period_start, period_end = df, dt
        days = (period_end - period_start).days + 1
    else:
        if days not in (7, 30, 90):
            days = 7
        period_start = today - timedelta(days=days - 1)
        period_end = today
    month_start = today.replace(day=1)

    # ---- headline numbers ------------------------------------------------
    period_expenses = (
        db.query(func.coalesce(func.sum(models.Expense.amount), 0))
        .filter(models.Expense.is_voided.is_(False), models.Expense.expense_date.between(period_start, period_end))
        .scalar()
    )
    period_expenses = Decimal(str(period_expenses or 0))
    period_profit = _profit_between(db, period_start, period_end)

    kpi = {
        "today": _sales_between(db, today, today),
        "month": _sales_between(db, month_start, today),
        "period": _sales_between(db, period_start, period_end),
        "profit": period_profit,
        "expenses": period_expenses,
        "net_profit": period_profit - period_expenses,
    }

    # ---- period-over-period comparison ("are we moving or not") ----------
    # Compare this window against the immediately preceding one of equal length.
    span = (period_end - period_start).days + 1
    prev_end = period_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span - 1)
    prev_sales = _sales_between(db, prev_start, prev_end)
    prev_profit = _profit_between(db, prev_start, prev_end)
    prev_expenses = Decimal(str(
        db.query(func.coalesce(func.sum(models.Expense.amount), 0))
        .filter(models.Expense.is_voided.is_(False), models.Expense.expense_date.between(prev_start, prev_end))
        .scalar() or 0
    ))
    compare = {
        "prev_start": prev_start, "prev_end": prev_end,
        "sales": {"now": kpi["period"], "prev": prev_sales, "pct": _pct_change(kpi["period"], prev_sales)},
        "profit": {"now": period_profit, "prev": prev_profit, "pct": _pct_change(period_profit, prev_profit)},
        "expenses": {"now": period_expenses, "prev": prev_expenses, "pct": _pct_change(period_expenses, prev_expenses)},
        "net_profit": {"now": kpi["net_profit"], "prev": prev_profit - prev_expenses,
                       "pct": _pct_change(kpi["net_profit"], prev_profit - prev_expenses)},
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
    # Bars stay daily at any range length (so hovering still shows the exact
    # day), but labels are thinned to at most MAX_TREND_LABELS — otherwise a
    # 30- or 90-day range crams enough date text under the bars that adjacent
    # labels visually run into each other (each bar column is only as wide as
    # 1/days of the card, and label text ignores that width once it's dense).
    rows = (
        db.query(
            _local_date(models.Sale.created_at).label("d"),
            func.coalesce(func.sum(models.Sale.total), 0),
        )
        .filter(
            _local_date(models.Sale.created_at).between(period_start, period_end),
            models.Sale.is_voided.is_(False),
        )
        .group_by("d")
        .all()
    )
    by_day = {r[0]: Decimal(str(r[1] or 0)) for r in rows}
    label_stride = max(1, -(-days // MAX_TREND_LABELS))  # ceil(days / MAX_TREND_LABELS)
    # The last day always gets a label (so the chart reads up to "today"), but
    # if the stride would also label the day right before it, that pair
    # collides into unreadable overlapping text — drop the stride one so only
    # the forced last label remains.
    last_i = days - 1
    show_indices = set(range(0, days, label_stride))
    show_indices.add(last_i)
    if label_stride > 1 and (last_i - 1) in show_indices:
        show_indices.discard(last_i - 1)
    trend = []
    for i in range(days):
        d = period_start + timedelta(days=i)
        show_label = i in show_indices
        trend.append({
            "date": d, "label": d.strftime("%b %d"), "short": d.strftime("%a"),
            "total": by_day.get(d, ZERO), "show_label": show_label,
        })
    trend_max = max([t["total"] for t in trend] + [ZERO])
    for t in trend:
        t["pct"] = float(t["total"] / trend_max * 100) if trend_max > 0 else 0.0
        t["is_today"] = t["date"] == today

    # ---- payment method mix ---------------------------------------------
    pay_rows = (
        db.query(models.Payment.method, func.coalesce(func.sum(models.Payment.amount), 0))
        .join(models.Sale, models.Payment.sale_id == models.Sale.id)
        .filter(_local_date(models.Sale.created_at).between(period_start, period_end))
        .group_by(models.Payment.method)
        .all()
    )
    labels = {
        "cash": "Cash", "gcash": "GCash", "maya": "Maya", "other_ewallet": "Other E-Wallet",
        "card": "Card", "bank_transfer": "Bank Transfer", "receivable": "Receivable",
    }
    colors = {
        "cash": "#16a34a", "gcash": "#2563eb", "maya": "#059669", "other_ewallet": "#65a30d",
        "card": "#7c3aed", "bank_transfer": "#0891b2", "receivable": "#d97706",
    }
    pay_totals = {m: Decimal(str(a or 0)) for m, a in pay_rows}
    # Change is always handed back in physical cash regardless of which method
    # funded the original payment (see pos.py) — net it out of the cash
    # bucket, or a day with any overpaid split/exchange would overstate cash.
    change_given = Decimal(str(
        db.query(func.coalesce(func.sum(models.Sale.change_amount), 0))
        .filter(
            _local_date(models.Sale.created_at).between(period_start, period_end),
            models.Sale.is_voided.is_(False),
        )
        .scalar() or 0
    ))
    if "cash" in pay_totals or change_given > 0:
        pay_totals["cash"] = pay_totals.get("cash", ZERO) - change_given
    pay_total = sum(pay_totals.values(), ZERO)
    payments = []
    for method, amt in sorted(pay_totals.items(), key=lambda r: float(r[1] or 0), reverse=True):
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
        .filter(models.Product.is_active.is_(True), low_stock_expr(settings_store.default_low_stock_pct()))
        .order_by(models.Product.name)
        .all()
    )
    no_cost = (
        db.query(func.count(models.Product.id))
        .filter(models.Product.is_active.is_(True), models.Product.cost_price <= 0)
        .scalar()
    ) or 0

    # Post-dated cheques due within 3 days or already overdue (received or issued).
    pdc_alert_count = (
        db.query(func.count(models.PostDatedCheque.id))
        .filter(
            models.PostDatedCheque.status == "pending",
            models.PostDatedCheque.cheque_date <= today + timedelta(days=3),
        )
        .scalar()
    ) or 0

    # ---- credit due ------------------------------------------------------
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
    credit_total = ZERO
    ar_aging = {b: ZERO for b in AGING_BUCKETS}
    horizon = today + timedelta(days=DUE_SOON_DAYS)
    for sale, paid in credit_rows:
        outstanding = Decimal(str(sale.receivable_amount or 0)) - Decimal(str(paid or 0))
        if outstanding <= 0:
            continue
        credit_total += outstanding
        ar_aging[_aging_bucket(sale.due_date, today)] += outstanding
        if not sale.due_date:
            continue
        entry = {"sale": sale, "outstanding": outstanding, "days": (sale.due_date - today).days}
        if sale.due_date < today:
            overdue.append(entry)
        elif sale.due_date <= horizon:
            due_soon.append(entry)

    # Overdue first (more urgent), then due-soon — same order the table
    # already showed, just paginated now instead of dumping every row at
    # once when a lot of customers are behind.
    credit_alerts = overdue + due_soon
    credit_pages = max((len(credit_alerts) + CREDIT_ALERT_PAGE_SIZE - 1) // CREDIT_ALERT_PAGE_SIZE, 1)
    credit_page = min(max(credit_page, 1), credit_pages)
    credit_alerts_page = credit_alerts[
        (credit_page - 1) * CREDIT_ALERT_PAGE_SIZE: credit_page * CREDIT_ALERT_PAGE_SIZE
    ]

    # ---- payables (what we owe suppliers) ---------------------------------
    # A 'confirmed' receive-type purchase IS the payable — goods/cost already
    # moved, payment hasn't (fully). Outstanding nets out PurchaseSettlement
    # the same way receivables net out ReceivableSettlement above, via the
    # helpers /purchases/payables/aging already uses — a purchase paid down
    # partway shouldn't still count at its full original total here.
    open_purchases = (
        db.query(models.Purchase)
        .filter(models.Purchase.txn_type == "receive", models.Purchase.status == "confirmed")
        .all()
    )
    purchase_settled = _settled_for_purchases(db, [p.id for p in open_purchases])
    payables_total = ZERO
    payables_overdue = 0
    ap_aging = {b: ZERO for b in AGING_BUCKETS}
    for p in open_purchases:
        outstanding = _purchase_outstanding(p, purchase_settled)
        if outstanding <= 0:
            continue
        payables_total += outstanding
        bucket = _aging_bucket(p.due_date, today)
        ap_aging[bucket] += outstanding
        if bucket != "current":
            payables_overdue += 1

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
            models.Sale.is_voided.is_(False),
            _local_date(models.Sale.created_at).between(period_start, period_end),
        )
        .group_by(models.SaleLine.product_id, models.SaleLine.product_name)
        .all()
    )
    top_revenue = sorted(perf, key=lambda r: float(r.revenue or 0), reverse=True)[:5]

    # Deliveries already received, most recent first — every purchase here
    # is something that already happened (stock and cost already moved),
    # not something still awaited, matching how purchases get entered here.
    recent_purchases = (
        db.query(models.Purchase)
        .filter(models.Purchase.txn_type == "receive")
        .order_by(models.Purchase.id.desc())
        .limit(8)
        .all()
    )

    # Dead stock: on hand but nothing sold in the last 30 days.
    sold_ids = {
        r[0] for r in db.query(models.SaleLine.product_id)
        .join(models.Sale, models.SaleLine.sale_id == models.Sale.id)
        .filter(
            _local_date(models.Sale.created_at) >= today - timedelta(days=30),
            models.Sale.is_voided.is_(False),
        )
        .distinct()
        .all()
        if r[0]
    }
    dead_q = db.query(models.Product).filter(models.Product.is_active.is_(True), _qty_expr() > 0)
    if sold_ids:
        dead_q = dead_q.filter(~models.Product.id.in_(sold_ids))
    dead_stock = dead_q.order_by(models.Product.name).limit(8).all()

    recent = (
        db.query(models.Sale)
        .filter(models.Sale.is_voided.is_(False))
        .order_by(models.Sale.id.desc())
        .limit(10)
        .all()
    )

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
            "custom": custom, "period_start": period_start, "period_end": period_end,
            "date_from": date_from, "date_to": date_to,
            "kpi": kpi, "compare": compare, "inventory": inventory,
            "trend": trend, "trend_max": trend_max,
            "payments": payments, "pay_total": pay_total,
            "out_of_stock": out_of_stock, "low_stock": low_stock, "no_cost": no_cost,
            "pdc_alert_count": pdc_alert_count,
            "credit_total": credit_total, "due_soon": due_soon, "overdue": overdue,
            "credit_alerts_page": credit_alerts_page, "credit_page": credit_page, "credit_pages": credit_pages,
            "payables_total": payables_total, "payables_overdue": payables_overdue,
            "ar_aging_rows": _aging_rows(ar_aging, credit_total),
            "ap_aging_rows": _aging_rows(ap_aging, payables_total),
            "top_revenue": top_revenue, "recent_purchases": recent_purchases, "dead_stock": dead_stock,
            "recent": recent, "backup": backup_info, "backup_stale_days": BACKUP_STALE_DAYS,
        },
    )
