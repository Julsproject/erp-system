"""Store display: a read-only wall screen showing what's moving right now.

Meant for a spare tablet or TV left running in the shop, not for someone to
browse. It signs in once as a `display` account (see main.confine_display,
which pins that account here and blocks every write), refreshes itself, and
otherwise needs no attention.

Two modes, chosen by URL so a device can simply be bookmarked:

    /display            full — includes peso amounts
    /display?money=0    figures hidden — safe where customers can see it

The money switch exists because a screen at the front counter is visible to
customers and suppliers. Transaction counts and stock alerts are fine there;
the day's takings are not necessarily something you want a supplier reading
before they quote you.

Cost and margin never appear in either mode. Those belong on the Dashboard,
behind a real login.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models, settings_store
from .database import get_db
from .deps import get_current_user, is_display, is_staff
from .products import low_stock_expr
from .templating import templates

router = APIRouter()

MANILA = ZoneInfo("Asia/Manila")
ZERO = Decimal("0")

RECENT_LIMIT = 8
TOP_LIMIT = 6
ATTENTION_LIMIT = 10


def _now():
    return datetime.now(MANILA)


def _local_date(col):
    return func.date(func.timezone("Asia/Manila", col))


@router.get("/display", response_class=HTMLResponse)
def store_display(
    request: Request,
    money: int = 1,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """The screen. Also openable by staff/admin so the shop owner can preview
    what the wall is showing without logging out of their own account."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not (is_display(user) or is_staff(user)):
        return RedirectResponse("/pos", status_code=302)

    show_money = bool(money)
    today = _now().date()

    # ---- today's activity -------------------------------------------------
    # Voided sales are excluded, same as every other report — a screen that
    # counts cancelled transactions would quietly disagree with the books.
    todays_sales = (
        db.query(models.Sale)
        .filter(
            models.Sale.is_voided.is_(False),
            _local_date(models.Sale.created_at) == today,
        )
        .order_by(models.Sale.id.desc())
        .all()
    )
    sales_count = sum(1 for s in todays_sales if s.txn_type == "sale")
    sales_total = sum((s.total or ZERO for s in todays_sales), ZERO)

    recent = todays_sales[:RECENT_LIMIT]

    # ---- what's actually moving today -------------------------------------
    cogs_free_rows = (
        db.query(
            models.SaleLine.product_name,
            func.coalesce(func.sum(models.SaleLine.qty), 0).label("qty"),
            func.coalesce(func.sum(models.SaleLine.line_total), 0).label("revenue"),
        )
        .join(models.Sale, models.SaleLine.sale_id == models.Sale.id)
        .filter(
            models.Sale.txn_type == "sale",
            models.Sale.is_voided.is_(False),
            _local_date(models.Sale.created_at) == today,
        )
        .group_by(models.SaleLine.product_name)
        .all()
    )
    top_movers = sorted(
        [
            {
                "name": r.product_name,
                "qty": Decimal(str(r.qty or 0)),
                "revenue": Decimal(str(r.revenue or 0)),
            }
            for r in cogs_free_rows
        ],
        key=lambda r: float(r["revenue"]),
        reverse=True,
    )[:TOP_LIMIT]

    # ---- what the floor should act on -------------------------------------
    qty_expr = models.Product.beginning_stock + models.Product.stock_qty
    out_of_stock = (
        db.query(models.Product)
        .filter(models.Product.is_active.is_(True), qty_expr <= 0)
        .order_by(models.Product.name)
        .limit(ATTENTION_LIMIT)
        .all()
    )
    low_stock = (
        db.query(models.Product)
        .filter(
            models.Product.is_active.is_(True),
            low_stock_expr(settings_store.default_low_stock_pct()),
        )
        .order_by(models.Product.name)
        .limit(ATTENTION_LIMIT)
        .all()
    )

    return templates.TemplateResponse(
        "display.html",
        {
            "request": request,
            "app_name": request.app.title,
            "user": user,
            "show_money": show_money,
            "now": _now(),
            "today": today,
            "sales_count": sales_count,
            "sales_total": sales_total,
            "recent": recent,
            "top_movers": top_movers,
            "out_of_stock": out_of_stock,
            "low_stock": low_stock,
            "is_preview": is_staff(user),
        },
    )
