"""Reports: cross-module summaries that don't live neatly in one module —
Profit & Loss (ties Sales, COGS and Expenses together) and Inventory
Valuation. The hub also points at the exportable lists other modules
already have (Sales, Expenses, Purchases, Cheques).
"""
import io
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models, settings_store
from .database import get_db
from .deps import get_current_user, is_admin
from .templating import templates

router = APIRouter()

MANILA = ZoneInfo("Asia/Manila")
ZERO = Decimal("0")


def _today() -> date:
    return datetime.now(MANILA).date()


def _local_date(col):
    return func.date(func.timezone("Asia/Manila", col))


def _parse_date(s: str):
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def _resolve_period(days: int, date_from: str, date_to: str):
    """Same custom-range-overrides-preset logic as the Dashboard, duplicated
    here rather than imported since each module owns its own small helpers."""
    today = _today()
    df, dt = _parse_date(date_from), _parse_date(date_to)
    custom = bool(df and dt)
    if custom:
        if dt > today:
            dt = today
        if df > dt:
            df, dt = dt, df
        if (dt - df).days > 365:
            df = dt - timedelta(days=365)
        return df, dt, custom
    if days not in (7, 30, 90):
        days = 30
    return today - timedelta(days=days - 1), today, custom


@router.get("/reports", response_class=HTMLResponse)
def reports_hub(request: Request, user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    return templates.TemplateResponse(
        "reports/hub.html",
        {"request": request, "app_name": request.app.title, "user": user},
    )


def _pl_data(db: Session, period_start: date, period_end: date):
    """Same formulas the Dashboard uses, so the numbers agree with what the
    owner already sees there: Revenue = net sales (sale + refund + exchange
    totals); Gross Profit = revenue from 'sale' lines minus their frozen cost."""
    revenue = (
        db.query(func.coalesce(func.sum(models.Sale.total), 0))
        .filter(_local_date(models.Sale.created_at).between(period_start, period_end))
        .scalar()
    )
    revenue = Decimal(str(revenue or 0))

    cogs_expr = models.SaleLine.qty * models.SaleLine.unit_factor * models.SaleLine.unit_cost
    gross_profit = (
        db.query(func.coalesce(func.sum(models.SaleLine.line_total - cogs_expr), 0))
        .join(models.Sale, models.SaleLine.sale_id == models.Sale.id)
        .filter(models.Sale.txn_type == "sale", _local_date(models.Sale.created_at).between(period_start, period_end))
        .scalar()
    )
    gross_profit = Decimal(str(gross_profit or 0))

    expense_rows = (
        db.query(models.ExpenseCategory.name, func.coalesce(func.sum(models.Expense.amount), 0))
        .select_from(models.Expense)
        .outerjoin(models.ExpenseCategory, models.Expense.category_id == models.ExpenseCategory.id)
        .filter(models.Expense.is_voided.is_(False), models.Expense.expense_date.between(period_start, period_end))
        .group_by(models.ExpenseCategory.name)
        .all()
    )
    expenses_by_category = sorted(
        [{"name": name or "Uncategorized", "amount": Decimal(str(amt or 0))} for name, amt in expense_rows],
        key=lambda r: r["amount"], reverse=True,
    )
    total_expenses = sum((r["amount"] for r in expenses_by_category), ZERO)

    inventory_adjustment_total = (
        db.query(func.coalesce(func.sum(models.StockMovement.value), 0))
        .filter(models.StockMovement.reason.in_(("adjustment", "stock_count")),
                _local_date(models.StockMovement.created_at).between(period_start, period_end))
        .scalar()
    )
    inventory_adjustment_total = Decimal(str(inventory_adjustment_total or 0))

    return {
        "revenue": revenue,
        "gross_profit": gross_profit,
        "expenses_by_category": expenses_by_category,
        "total_expenses": total_expenses,
        "inventory_adjustment_total": inventory_adjustment_total,
        "net_profit": gross_profit - total_expenses + inventory_adjustment_total,
    }


@router.get("/reports/profit-loss", response_class=HTMLResponse)
def profit_loss(
    request: Request,
    days: int = 30,
    date_from: str = "",
    date_to: str = "",
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)

    period_start, period_end, custom = _resolve_period(days, date_from, date_to)
    data = _pl_data(db, period_start, period_end)

    return templates.TemplateResponse(
        "reports/profit_loss.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "days": days, "date_from": date_from, "date_to": date_to,
            "period_start": period_start, "period_end": period_end, "custom": custom,
            **data,
        },
    )


@router.get("/reports/profit-loss/export")
def export_profit_loss(
    days: int = 30,
    date_from: str = "",
    date_to: str = "",
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)

    period_start, period_end, _ = _resolve_period(days, date_from, date_to)
    data = _pl_data(db, period_start, period_end)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "P&L"
    header_fill = PatternFill("solid", fgColor="1F6FEB")

    def header_row(cells):
        ws.append(cells)
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = header_fill

    ws.append([f"Profit & Loss — {period_start.isoformat()} to {period_end.isoformat()}"])
    ws.append([])
    header_row(["Line", "Amount"])
    ws.append(["Revenue (net of refunds/exchanges)", float(data["revenue"])])
    ws.append(["Gross Profit (from goods sold)", float(data["gross_profit"])])
    ws.append([])
    header_row(["Expenses by category", "Amount"])
    for row in data["expenses_by_category"]:
        ws.append([row["name"], float(row["amount"])])
    ws.append(["Total Expenses", float(data["total_expenses"])])
    ws.append([])
    ws.append(["Inventory Shrinkage / Gain (adjustments & stock counts)", float(data["inventory_adjustment_total"])])
    ws.append([])
    ws.append(["Net Profit (Gross Profit − Expenses + Inventory Adjustments)", float(data["net_profit"])])

    for cell in ws["A"]:
        if cell.value in ("Total Expenses", "Net Profit (Gross Profit − Expenses + Inventory Adjustments)"):
            cell.font = Font(bold=True)
    ws.column_dimensions["A"].width = 40
    ws.column_dimensions["B"].width = 18

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"profit_loss_{period_start.isoformat()}_{period_end.isoformat()}.xlsx"
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _qty_expr():
    return models.Product.beginning_stock + models.Product.stock_qty


def _valuation_rows(db: Session):
    products = (
        db.query(models.Product)
        .filter(models.Product.is_active.is_(True))
        .order_by(models.Product.name)
        .all()
    )
    rows = []
    for p in products:
        qty = Decimal(str(p.total_qty or 0))
        cost_val = qty * Decimal(str(p.cost_price or 0))
        retail_val = qty * Decimal(str(p.selling_price or 0))
        rows.append({
            "product": p,
            "category": p.category.name if p.category else "Uncategorized",
            "qty": qty,
            "cost_value": cost_val,
            "retail_value": retail_val,
        })
    return rows


def _sales_by_product(db: Session, period_start: date, period_end: date):
    """Per-product sales in the window: units sold, revenue and gross profit.
    Uses 'sale' lines only (same basis as the Dashboard's top-sellers), so a
    product's movement here reads as gross demand, not net-of-returns."""
    cogs_expr = models.SaleLine.qty * models.SaleLine.unit_factor * models.SaleLine.unit_cost
    rows = (
        db.query(
            models.SaleLine.product_name,
            func.coalesce(func.sum(models.SaleLine.qty), 0).label("qty"),
            func.coalesce(func.sum(models.SaleLine.line_total), 0).label("revenue"),
            func.coalesce(func.sum(models.SaleLine.line_total - cogs_expr), 0).label("profit"),
        )
        .join(models.Sale, models.SaleLine.sale_id == models.Sale.id)
        .filter(models.Sale.txn_type == "sale", _local_date(models.Sale.created_at).between(period_start, period_end))
        .group_by(models.SaleLine.product_name)
        .all()
    )
    out = [
        {
            "name": r.product_name,
            "qty": Decimal(str(r.qty or 0)),
            "revenue": Decimal(str(r.revenue or 0)),
            "profit": Decimal(str(r.profit or 0)),
        }
        for r in rows
    ]
    out.sort(key=lambda r: r["revenue"], reverse=True)
    return out


@router.get("/reports/sales-by-product", response_class=HTMLResponse)
def sales_by_product(
    request: Request,
    days: int = 30,
    date_from: str = "",
    date_to: str = "",
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)

    period_start, period_end, custom = _resolve_period(days, date_from, date_to)
    rows = _sales_by_product(db, period_start, period_end)
    totals = {
        "qty": sum((r["qty"] for r in rows), ZERO),
        "revenue": sum((r["revenue"] for r in rows), ZERO),
        "profit": sum((r["profit"] for r in rows), ZERO),
    }
    return templates.TemplateResponse(
        "reports/sales_by_product.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "days": days, "date_from": date_from, "date_to": date_to,
            "period_start": period_start, "period_end": period_end, "custom": custom,
            "rows": rows, "totals": totals,
        },
    )


@router.get("/reports/sales-by-product/export")
def export_sales_by_product(
    days: int = 30,
    date_from: str = "",
    date_to: str = "",
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)

    period_start, period_end, _ = _resolve_period(days, date_from, date_to)
    rows = _sales_by_product(db, period_start, period_end)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sales by Product"
    headers = ["Product", "Units Sold", "Revenue", "Gross Profit"]
    ws.append(headers)
    header_fill = PatternFill("solid", fgColor="1F6FEB")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
    for r in rows:
        ws.append([r["name"], float(r["qty"]), float(r["revenue"]), float(r["profit"])])
    widths = [32, 14, 16, 16]
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"sales_by_product_{period_start.isoformat()}_{period_end.isoformat()}.xlsx"
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _low_margin_rows(db: Session, threshold: float):
    """Same rule as the 'thin margin' Notification: profitable products
    (selling above cost) whose true margin still falls under the shop's
    target. Products at/below cost are a harder problem — they're already
    surfaced separately — so they're excluded here."""
    products = (
        db.query(models.Product)
        .filter(
            models.Product.is_active.is_(True),
            models.Product.cost_price > 0,
            models.Product.selling_price > models.Product.cost_price,
        )
        .order_by(models.Product.name)
        .all()
    )
    rows = []
    for p in products:
        price = Decimal(str(p.selling_price or 0))
        cost = Decimal(str(p.cost_price or 0))
        margin_pct = float((price - cost) / price * 100) if price > 0 else 0.0
        if margin_pct < threshold:
            rows.append({
                "product": p,
                "cost": cost,
                "price": price,
                "margin_pct": margin_pct,
                "gap_pct": threshold - margin_pct,
            })
    rows.sort(key=lambda r: r["margin_pct"])
    return rows


@router.get("/reports/low-margin", response_class=HTMLResponse)
def low_margin_report(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)

    threshold = settings_store.min_margin_pct()
    rows = _low_margin_rows(db, threshold) if threshold is not None else []

    return templates.TemplateResponse(
        "reports/low_margin.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "rows": rows, "threshold": threshold,
        },
    )


@router.get("/reports/low-margin/export")
def export_low_margin(db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)

    threshold = settings_store.min_margin_pct()
    rows = _low_margin_rows(db, threshold) if threshold is not None else []

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Low Margin"
    ws.append([f"Products below {threshold:g}% margin target" if threshold is not None else "No margin target set"])
    ws.append([])
    headers = ["Product", "Cost", "Selling Price", "Margin %", "Gap to Target %"]
    ws.append(headers)
    header_fill = PatternFill("solid", fgColor="1F6FEB")
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
    for r in rows:
        ws.append([r["product"].name, float(r["cost"]), float(r["price"]), round(r["margin_pct"], 1), round(r["gap_pct"], 1)])
    widths = [30, 14, 14, 12, 16]
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A4"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="low_margin_{_today().isoformat()}.xlsx"'},
    )


@router.get("/reports/inventory-valuation", response_class=HTMLResponse)
def inventory_valuation(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)

    rows = _valuation_rows(db)
    total_cost = sum((r["cost_value"] for r in rows), ZERO)
    total_retail = sum((r["retail_value"] for r in rows), ZERO)

    by_cat = {}
    for r in rows:
        c = by_cat.setdefault(r["category"], {"category": r["category"], "cost_value": ZERO, "retail_value": ZERO, "count": 0})
        c["cost_value"] += r["cost_value"]
        c["retail_value"] += r["retail_value"]
        c["count"] += 1
    by_category = sorted(by_cat.values(), key=lambda r: r["cost_value"], reverse=True)

    return templates.TemplateResponse(
        "reports/inventory_valuation.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "rows": rows, "by_category": by_category,
            "total_cost": total_cost, "total_retail": total_retail,
            "today": _today(),
        },
    )


@router.get("/reports/inventory-valuation/export")
def export_inventory_valuation(db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)

    rows = _valuation_rows(db)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Inventory Valuation"
    headers = ["Product", "Category", "Qty on Hand", "Cost Price", "Cost Value", "Selling Price", "Retail Value"]
    ws.append(headers)
    header_fill = PatternFill("solid", fgColor="1F6FEB")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill

    for r in rows:
        p = r["product"]
        ws.append([
            p.name, r["category"], float(r["qty"]),
            float(p.cost_price or 0), float(r["cost_value"]),
            float(p.selling_price or 0), float(r["retail_value"]),
        ])

    widths = [28, 18, 14, 14, 14, 14, 14]
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="inventory_valuation_{_today().isoformat()}.xlsx"'},
    )


def _inventory_adjustment_rows(db: Session, period_start: date, period_end: date):
    """Every manual stock edit or completed stock count in the window,
    valued at the product's cost at the time — negative value = shrinkage
    (loss), positive = a find (gain)."""
    rows = (
        db.query(models.StockMovement, models.Product)
        .join(models.Product, models.StockMovement.product_id == models.Product.id)
        .filter(
            models.StockMovement.reason.in_(("adjustment", "stock_count")),
            _local_date(models.StockMovement.created_at).between(period_start, period_end),
        )
        .order_by(models.StockMovement.created_at.desc())
        .all()
    )
    out = []
    for mv, product in rows:
        out.append({
            "created_at": mv.created_at,
            "product_name": product.name,
            "reason": "Stock count" if mv.reason == "stock_count" else "Manual edit",
            "note": mv.note or "—",
            "ref": mv.ref or "—",
            "qty_base": Decimal(str(mv.qty_base or 0)),
            "unit_cost": Decimal(str(mv.unit_cost or 0)),
            "value": Decimal(str(mv.value or 0)),
        })
    return out


@router.get("/reports/inventory-adjustments", response_class=HTMLResponse)
def inventory_adjustments(
    request: Request,
    days: int = 30,
    date_from: str = "",
    date_to: str = "",
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)

    period_start, period_end, custom = _resolve_period(days, date_from, date_to)
    rows = _inventory_adjustment_rows(db, period_start, period_end)
    loss_total = sum((r["value"] for r in rows if r["value"] < 0), ZERO)
    gain_total = sum((r["value"] for r in rows if r["value"] > 0), ZERO)
    net_total = loss_total + gain_total

    today = _today()
    month_start = today.replace(day=1)
    this_month = custom and period_start == month_start and period_end == today

    return templates.TemplateResponse(
        "reports/inventory_adjustments.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "days": days, "date_from": date_from, "date_to": date_to,
            "period_start": period_start, "period_end": period_end, "custom": custom,
            "month_start": month_start, "today": today, "this_month": this_month,
            "rows": rows, "loss_total": loss_total, "gain_total": gain_total, "net_total": net_total,
        },
    )
