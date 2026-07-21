"""Cash drawer shifts.

A cashier must open the drawer (declare the starting cash) before using the
system. At the end of the day they count the drawer and the system compares it
against what it expects, showing over/short. Admins skip all of this.
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models
from .database import get_db
from .deps import get_current_user
from .templating import templates

router = APIRouter()

CENTS = Decimal("0.01")
ZERO = Decimal("0")


def _money(value, default="0") -> Decimal:
    try:
        d = Decimal(str(value).strip().replace(",", "") or default)
    except (InvalidOperation, AttributeError, ValueError):
        d = Decimal(default)
    return d.quantize(CENTS, rounding=ROUND_HALF_UP)


def is_admin(user) -> bool:
    return (user.role or "").lower() == "admin"


def open_shift_for(db: Session, user_id: int):
    return (
        db.query(models.CashShift)
        .filter(models.CashShift.user_id == user_id, models.CashShift.closed_at.is_(None))
        .order_by(models.CashShift.id.desc())
        .first()
    )


def shift_summary(db: Session, shift: models.CashShift) -> dict:
    """Work out what should physically be in the drawer right now."""
    def in_window(query, ts_col):
        query = query.filter(ts_col >= shift.opened_at)
        if shift.closed_at:
            query = query.filter(ts_col <= shift.closed_at)
        return query

    # Cash taken in from sales by this cashier during the shift.
    cash_in = in_window(
        db.query(func.coalesce(func.sum(models.Payment.amount), 0))
        .join(models.Sale, models.Payment.sale_id == models.Sale.id)
        .filter(models.Payment.method == "cash", models.Sale.cashier_id == shift.user_id),
        models.Sale.created_at,
    ).scalar()

    # Cash handed back: change on sales, plus cash paid out on refunds/exchanges.
    cash_out = in_window(
        db.query(func.coalesce(func.sum(models.Sale.change_amount), 0))
        .filter(models.Sale.cashier_id == shift.user_id),
        models.Sale.created_at,
    ).scalar()

    # Utang collected in cash during the shift.
    collections = in_window(
        db.query(func.coalesce(func.sum(models.ReceivableSettlement.amount), 0))
        .filter(
            models.ReceivableSettlement.method == "cash",
            models.ReceivableSettlement.cashier_id == shift.user_id,
        ),
        models.ReceivableSettlement.created_at,
    ).scalar()

    # Non-cash tenders, shown for information only (they never hit the drawer).
    non_cash_rows = in_window(
        db.query(models.Payment.method, func.coalesce(func.sum(models.Payment.amount), 0))
        .join(models.Sale, models.Payment.sale_id == models.Sale.id)
        .filter(models.Payment.method != "cash", models.Sale.cashier_id == shift.user_id)
        .group_by(models.Payment.method),
        models.Sale.created_at,
    ).all()

    txn_count = in_window(
        db.query(func.count(models.Sale.id)).filter(models.Sale.cashier_id == shift.user_id),
        models.Sale.created_at,
    ).scalar()

    opening = Decimal(str(shift.opening_amount or 0))
    cash_in = Decimal(str(cash_in or 0))
    cash_out = Decimal(str(cash_out or 0))
    collections = Decimal(str(collections or 0))
    expected = opening + cash_in - cash_out + collections

    labels = {"gcash": "GCash", "card": "Card", "bank_transfer": "Bank Transfer", "receivable": "Receivable (Utang)"}
    return {
        "opening": opening,
        "cash_in": cash_in,
        "cash_out": cash_out,
        "collections": collections,
        "expected": expected,
        "cash_net": cash_in - cash_out + collections,
        "txn_count": txn_count or 0,
        "non_cash": [{"label": labels.get(m, m.title()), "amount": Decimal(str(a or 0))} for m, a in non_cash_rows],
    }


@router.get("/shift/open", response_class=HTMLResponse)
def open_form(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if is_admin(user):
        return RedirectResponse("/shifts", status_code=302)
    if open_shift_for(db, user.id):
        return RedirectResponse("/shift", status_code=302)
    last = (
        db.query(models.CashShift)
        .filter(models.CashShift.user_id == user.id)
        .order_by(models.CashShift.id.desc())
        .first()
    )
    return templates.TemplateResponse(
        "shifts/open.html",
        {"request": request, "app_name": request.app.title, "user": user, "last": last, "error": None},
    )


@router.post("/shift/open")
async def open_submit(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if is_admin(user) or open_shift_for(db, user.id):
        return RedirectResponse("/shift", status_code=302)
    form = await request.form()
    raw = (form.get("opening_amount") or "").strip()
    if raw == "":
        return templates.TemplateResponse(
            "shifts/open.html",
            {"request": request, "app_name": request.app.title, "user": user, "last": None,
             "error": "Enter the amount of cash in the drawer (put 0 if it is empty)."},
        )
    shift = models.CashShift(user_id=user.id, opening_amount=_money(raw))
    db.add(shift)
    db.commit()
    return RedirectResponse("/", status_code=status.HTTP_302_FOUND)


@router.get("/shift", response_class=HTMLResponse)
def current_shift(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if is_admin(user):
        return RedirectResponse("/shifts", status_code=302)
    shift = open_shift_for(db, user.id)
    if not shift:
        return RedirectResponse("/shift/open", status_code=302)
    return templates.TemplateResponse(
        "shifts/current.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "shift": shift, "s": shift_summary(db, shift), "error": None},
    )


@router.post("/shift/close")
async def close_shift(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    shift = open_shift_for(db, user.id)
    if not shift:
        return RedirectResponse("/shift/open", status_code=302)
    form = await request.form()
    raw = (form.get("closing_amount") or "").strip()
    summary = shift_summary(db, shift)
    if raw == "":
        return templates.TemplateResponse(
            "shifts/current.html",
            {"request": request, "app_name": request.app.title, "user": user,
             "shift": shift, "s": summary, "error": "Enter the amount you counted in the drawer."},
        )
    counted = _money(raw)
    shift.closing_amount = counted
    shift.expected_amount = summary["expected"]
    shift.difference = counted - summary["expected"]
    shift.notes = (form.get("notes") or "").strip() or None
    shift.closed_at = func.now()
    db.commit()
    return RedirectResponse(f"/shifts/{shift.id}", status_code=status.HTTP_302_FOUND)


@router.get("/shifts", response_class=HTMLResponse)
def shift_list(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Admins see every cashier's drawer; a cashier sees only their own."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    query = db.query(models.CashShift)
    if not is_admin(user):
        query = query.filter(models.CashShift.user_id == user.id)
    shifts = query.order_by(models.CashShift.id.desc()).limit(100).all()

    rows = []
    for sh in shifts:
        rows.append({"shift": sh, "live": shift_summary(db, sh) if sh.is_open else None})
    return templates.TemplateResponse(
        "shifts/list.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "rows": rows, "is_admin": is_admin(user)},
    )


@router.get("/shifts/{shift_id:int}", response_class=HTMLResponse)
def shift_detail(shift_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    shift = db.get(models.CashShift, shift_id)
    if not shift or (not is_admin(user) and shift.user_id != user.id):
        return RedirectResponse("/shifts", status_code=302)
    return templates.TemplateResponse(
        "shifts/detail.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "shift": shift, "s": shift_summary(db, shift)},
    )
