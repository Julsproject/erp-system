"""Cash drawer shifts: an opening float declared at the start of a session,
and an end-of-shift physical count that's compared against what the recorded
transactions say should be there.

This is the physical-cash counterpart to the automatic Cashier Activity
history: Activity derives what *should* have happened from Sales/Payments;
a shift records what was *actually counted*, so a shortage or overage is
caught the same day instead of discovered weeks later.

A drawer session only ever applies to whoever is about to use the register —
enforced by gating `/pos` (see pos.pos_page), not by role, so an admin acting
as cashier is covered the same way. Opening one is a deliberate action, not
tied to login/logout, so stepping away and back doesn't start a new shift.
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import audit, models
from .database import get_db
from .deps import get_current_user, is_staff
from .templating import templates

router = APIRouter()

MANILA = ZoneInfo("Asia/Manila")
ZERO = Decimal("0")
PAGE_SIZE = 15


def _now():
    return datetime.now(MANILA)


def _dec(value, default="0") -> Decimal:
    try:
        return Decimal(str(value).strip().replace(",", "") or default)
    except (InvalidOperation, AttributeError, ValueError):
        return Decimal(default)


def get_open_shift(db: Session, cashier_id: int):
    return (
        db.query(models.CashierShift)
        .filter(models.CashierShift.cashier_id == cashier_id, models.CashierShift.status == "open")
        .order_by(models.CashierShift.id.desc())
        .first()
    )


def expected_cash_for(db: Session, cashier_id: int, start, end) -> Decimal:
    """Net cash movement in [start, end) for one cashier, added to the
    shift's opening float:

      + cash actually tendered (Payment.amount, method='cash') on sales/exchanges
      - change handed back (Sale.change_amount — always physical cash, per the
        convention set in pos.py: a refund's or overpaid exchange's change is
        cash out regardless of which method funded the original payment)
      + cash collected against credit/COD balances (ReceivableSettlement,
        method='cash') — a utang or COD collection puts real cash in the
        drawer just as much as a POS sale does, and would otherwise make every
        shift that collected any look artificially short.
    """
    cash_in = (
        db.query(func.coalesce(func.sum(models.Payment.amount), 0))
        .join(models.Sale, models.Payment.sale_id == models.Sale.id)
        .filter(
            models.Sale.cashier_id == cashier_id,
            models.Payment.method == "cash",
            models.Sale.created_at >= start, models.Sale.created_at < end,
        )
        .scalar()
    )
    change_out = (
        db.query(func.coalesce(func.sum(models.Sale.change_amount), 0))
        .filter(
            models.Sale.cashier_id == cashier_id,
            models.Sale.is_voided.is_(False),
            models.Sale.created_at >= start, models.Sale.created_at < end,
        )
        .scalar()
    )
    settled_cash = (
        db.query(func.coalesce(func.sum(models.ReceivableSettlement.amount), 0))
        .filter(
            models.ReceivableSettlement.cashier_id == cashier_id,
            models.ReceivableSettlement.method == "cash",
            models.ReceivableSettlement.created_at >= start, models.ReceivableSettlement.created_at < end,
        )
        .scalar()
    )
    net = Decimal(str(cash_in or 0)) - Decimal(str(change_out or 0)) + Decimal(str(settled_cash or 0))
    return net


@router.get("/shifts/open", response_class=HTMLResponse)
def open_shift_form(request: Request, next: str = "/pos", db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if get_open_shift(db, user.id):
        return RedirectResponse(next or "/pos", status_code=302)
    return templates.TemplateResponse(
        "shifts/open.html",
        {"request": request, "app_name": request.app.title, "user": user, "next": next or "/pos", "error": None},
    )


@router.post("/shifts/open")
async def open_shift(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if get_open_shift(db, user.id):
        form = await request.form()
        return RedirectResponse((form.get("next") or "/pos"), status_code=302)

    form = await request.form()
    opening = _dec(form.get("opening_amount"))
    if opening < 0:
        return templates.TemplateResponse(
            "shifts/open.html",
            {"request": request, "app_name": request.app.title, "user": user,
             "next": form.get("next") or "/pos", "error": "Enter a valid opening amount (0 or more)."},
        )
    shift = models.CashierShift(cashier_id=user.id, opening_amount=opening)
    db.add(shift)
    db.flush()
    audit.record(
        db, user=user, request=request, action="create", entity_type="shift",
        entity_id=shift.id, entity_label=f"Shift #{shift.id}",
        summary=f"{user.username} opened the drawer with {opening}",
    )
    db.commit()
    return RedirectResponse((form.get("next") or "/pos"), status_code=status.HTTP_302_FOUND)


@router.get("/shifts/current", response_class=HTMLResponse)
def current_shift(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    shift = get_open_shift(db, user.id)
    if not shift:
        return RedirectResponse("/shifts/open", status_code=302)

    running = shift.opening_amount + expected_cash_for(db, user.id, shift.opened_at, _now())
    accounts = db.query(models.BankAccount).filter(models.BankAccount.is_active.is_(True)).order_by(models.BankAccount.name).all()
    return templates.TemplateResponse(
        "shifts/current.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "shift": shift, "running": running, "accounts": accounts, "error": None},
    )


@router.post("/shifts/close")
async def close_shift(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    shift = get_open_shift(db, user.id)
    if not shift:
        return RedirectResponse("/shifts/open", status_code=302)

    form = await request.form()
    counted = form.get("counted_cash")
    if counted is None or str(counted).strip() == "":
        accounts = db.query(models.BankAccount).filter(models.BankAccount.is_active.is_(True)).order_by(models.BankAccount.name).all()
        running = shift.opening_amount + expected_cash_for(db, user.id, shift.opened_at, _now())
        return templates.TemplateResponse(
            "shifts/current.html",
            {"request": request, "app_name": request.app.title, "user": user,
             "shift": shift, "running": running, "accounts": accounts,
             "error": "Count the drawer and enter the amount before closing."},
        )
    counted = _dec(counted)

    now = _now()
    expected = shift.opening_amount + expected_cash_for(db, user.id, shift.opened_at, now)
    shift.expected_cash = expected
    shift.counted_cash = counted
    shift.variance = counted - expected
    shift.closed_at = now
    shift.status = "closed"
    shift.notes = (form.get("notes") or "").strip() or None

    deposit_amount = _dec(form.get("deposit_amount"))
    account_id = int(form.get("bank_account_id") or 0)
    if deposit_amount > 0 and account_id:
        account = db.get(models.BankAccount, account_id)
        if account:
            txn = models.BankTransaction(
                account_id=account.id, txn_type="deposit", amount=deposit_amount,
                txn_date=now.date(),
                description=f"Cash drawer deposit — shift #{shift.id} ({user.username})",
                created_by=user.id,
            )
            db.add(txn)
            db.flush()
            shift.bank_account_id = account.id
            shift.bank_transaction_id = txn.id
            shift.deposited_amount = deposit_amount

    audit.record(
        db, user=user, request=request, action="update", entity_type="shift",
        entity_id=shift.id, entity_label=f"Shift #{shift.id}",
        summary=(f"{user.username} closed the drawer — counted {counted}, expected {expected}, "
                 f"variance {shift.variance}"
                 + (f", deposited {deposit_amount}" if deposit_amount > 0 and account_id else "")),
    )
    db.commit()
    return RedirectResponse("/shifts/open", status_code=status.HTTP_302_FOUND)


@router.get("/shifts", response_class=HTMLResponse)
def shift_history(
    request: Request,
    cashier_id: int = 0,
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)

    from datetime import date as _date
    def _parse(s):
        try:
            return _date.fromisoformat(s) if s else None
        except ValueError:
            return None

    page = max(page, 1)
    query = db.query(models.CashierShift).filter(models.CashierShift.status == "closed")
    if cashier_id:
        query = query.filter(models.CashierShift.cashier_id == cashier_id)
    df, dt = _parse(date_from), _parse(date_to)
    if df:
        query = query.filter(func.date(models.CashierShift.closed_at) >= df)
    if dt:
        query = query.filter(func.date(models.CashierShift.closed_at) <= dt)

    total = query.count()
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, pages)
    shifts = (
        query.order_by(models.CashierShift.closed_at.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
        .all()
    )
    open_shifts = (
        db.query(models.CashierShift)
        .filter(models.CashierShift.status == "open")
        .order_by(models.CashierShift.opened_at.desc())
        .all()
    )
    users = db.query(models.User).order_by(models.User.username).all()
    variance_total = (
        query.with_entities(func.coalesce(func.sum(models.CashierShift.variance), 0)).scalar()
    ) or 0

    return templates.TemplateResponse(
        "shifts/list.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "shifts": shifts, "open_shifts": open_shifts, "users": users,
            "cashier_id": cashier_id, "date_from": date_from, "date_to": date_to,
            "page": page, "pages": pages, "total": total,
            "variance_total": Decimal(str(variance_total or 0)),
        },
    )
