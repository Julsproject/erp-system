"""Cashier activity: a plain history of what a cashier processed on a given day.

Nothing here blocks anyone from working and nothing needs to be manually
counted or declared — it's a read-only report computed straight from
Sales/Payments/Settlements already on file.
"""
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models
from .database import get_db
from .deps import get_current_user, is_admin
from .templating import templates

router = APIRouter()

MANILA = ZoneInfo("Asia/Manila")
METHOD_LABELS = {"cash": "Cash", "gcash": "GCash", "card": "Card", "bank_transfer": "Bank Transfer", "cheque": "Cheque", "receivable": "Receivable (Credit)"}


def _today() -> date:
    return datetime.now(MANILA).date()


def _parse_day(s: str):
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def _local_date(col):
    return func.date(func.timezone("Asia/Manila", col))


def _day_summary(db: Session, cashier_id: int, day: date) -> dict:
    """Everything this cashier processed on this calendar day."""
    sales = (
        db.query(models.Sale)
        .filter(models.Sale.cashier_id == cashier_id, _local_date(models.Sale.created_at) == day)
        .order_by(models.Sale.id)
        .all()
    )
    pay_rows = (
        db.query(models.Payment.method, func.coalesce(func.sum(models.Payment.amount), 0))
        .join(models.Sale, models.Payment.sale_id == models.Sale.id)
        .filter(models.Sale.cashier_id == cashier_id, _local_date(models.Sale.created_at) == day)
        .group_by(models.Payment.method)
        .all()
    )
    collections = (
        db.query(func.coalesce(func.sum(models.ReceivableSettlement.amount), 0))
        .filter(
            models.ReceivableSettlement.cashier_id == cashier_id,
            _local_date(models.ReceivableSettlement.created_at) == day,
        )
        .scalar()
    )
    by_method = [
        {"label": METHOD_LABELS.get(m, (m or "").title()), "amount": Decimal(str(a or 0))}
        for m, a in sorted(pay_rows, key=lambda r: float(r[1] or 0), reverse=True)
    ]
    net_total = sum((s.total or Decimal("0") for s in sales), Decimal("0"))
    return {
        "sales": sales,
        "count": len(sales),
        "net_total": net_total,
        "by_method": by_method,
        "collections": Decimal(str(collections or 0)),
    }


@router.get("/activity", response_class=HTMLResponse)
def my_activity(request: Request, day: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    """A cashier's own activity for a day (defaults to today)."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if is_admin(user):
        return RedirectResponse("/activity/all", status_code=302)
    d = _parse_day(day) or _today()
    summary = _day_summary(db, user.id, d)
    return templates.TemplateResponse(
        "activity/mine.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "day": d, "summary": summary, "viewing": None},
    )


@router.get("/activity/all", response_class=HTMLResponse)
def all_activity(request: Request, day: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Admin: every cashier's activity for a day, one row per cashier."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/activity", status_code=302)
    d = _parse_day(day) or _today()
    cashiers = db.query(models.User).filter(models.User.is_active.is_(True)).order_by(models.User.username).all()
    rows = []
    for c in cashiers:
        s = _day_summary(db, c.id, d)
        if s["count"] > 0:
            rows.append({"user": c, "summary": s})
    return templates.TemplateResponse(
        "activity/all.html",
        {"request": request, "app_name": request.app.title, "user": user, "day": d, "rows": rows},
    )


@router.get("/activity/user/{user_id:int}", response_class=HTMLResponse)
def user_activity(
    user_id: int,
    request: Request,
    day: str = "",
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Admin drilling into one cashier's day (or a cashier's own — same page)."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user) and user.id != user_id:
        return RedirectResponse("/activity", status_code=302)
    target = db.get(models.User, user_id)
    if not target:
        return RedirectResponse("/activity/all" if is_admin(user) else "/activity", status_code=302)
    d = _parse_day(day) or _today()
    summary = _day_summary(db, user_id, d)
    return templates.TemplateResponse(
        "activity/mine.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "day": d, "summary": summary, "viewing": target if is_admin(user) else None},
    )
