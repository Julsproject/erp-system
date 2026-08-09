"""Expenses: business costs that aren't inventory purchases — rent, utilities,
salaries, and so on. Unlike Purchases, an expense has no pending/confirmed
staging: recording one here means the money is already out the door.
"""
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from . import accounting, audit, models
from .database import get_db
from .deps import get_current_user, is_staff
from .templating import templates

router = APIRouter()

PAGE_SIZE = 15
# Same idea as backup.py's BACKUP_DIR — a host-backed volume so uploaded
# receipts survive a container rebuild, not local disk inside the container.
UPLOADS_DIR = Path("/uploads/expenses")
PAYMENT_METHODS = [
    ("cash", "Cash"), ("gcash", "GCash"), ("maya", "Maya"), ("other_ewallet", "Other E-Wallet"),
    ("bank_transfer", "Bank Transfer"), ("cheque", "Cheque"), ("petty_cash", "Petty Cash"),
    ("credit_card", "Credit Card"),
]
# payment_method values a paid_from_account can meaningfully attach to — any
# method where there's realistically more than one account it could be
# (several petty-cash funds, several bank accounts, several GCash numbers).
# Cheque and Credit Card stay on the generic AccountMapping — a cheque is
# already tracked per-instance via PDC, and Credit Card posts to one shared
# clearing account regardless of which physical card was used.
PAID_FROM_METHODS = ("petty_cash", "gcash", "maya", "other_ewallet", "cash", "bank_transfer")


def _dec(value, default="0") -> Decimal:
    try:
        return Decimal(str(value).strip().replace(",", "") or default)
    except (InvalidOperation, AttributeError, ValueError):
        return Decimal(default)


def _parse_date(s: str):
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def _get_or_create_category(db: Session, name: str):
    name = (name or "").strip()
    if not name:
        return None
    existing = db.query(models.ExpenseCategory).filter(func.lower(models.ExpenseCategory.name) == name.lower()).first()
    if existing:
        return existing
    cat = models.ExpenseCategory(name=name)
    db.add(cat)
    db.flush()
    return cat


@router.get("/expenses", response_class=HTMLResponse)
def list_expenses(
    request: Request,
    q: str = "",
    category_id: int = 0,
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
    q = (q or "").strip()
    page = max(page, 1)
    df, dt = _parse_date(date_from), _parse_date(date_to)

    query = db.query(models.Expense).filter(models.Expense.is_voided.is_(False))
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            models.Expense.payee.ilike(like),
            models.Expense.description.ilike(like),
            models.Expense.ref_no.ilike(like),
            models.Expense.reference_no.ilike(like),
        ))
    if category_id:
        query = query.filter(models.Expense.category_id == category_id)
    if df:
        query = query.filter(models.Expense.expense_date >= df)
    if dt:
        query = query.filter(models.Expense.expense_date <= dt)

    total_count, total_amount = query.with_entities(
        func.count(models.Expense.id), func.coalesce(func.sum(models.Expense.amount), 0)
    ).one()
    pages = max((total_count + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, pages)
    expenses = (
        query.order_by(models.Expense.expense_date.desc(), models.Expense.id.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
        .all()
    )
    categories = db.query(models.ExpenseCategory).order_by(models.ExpenseCategory.name).all()

    return templates.TemplateResponse(
        "expenses/list.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "expenses": expenses, "categories": categories, "category_id": category_id,
            "q": q, "date_from": date_from, "date_to": date_to,
            "total_count": total_count, "total_amount": Decimal(str(total_amount or 0)),
            "page": page, "pages": pages,
        },
    )


def _render_form(request, db, user, expense=None, error=None):
    categories = db.query(models.ExpenseCategory).order_by(models.ExpenseCategory.name).all()
    paid_from_accounts = (
        db.query(models.BankAccount)
        .filter(models.BankAccount.is_active.is_(True))
        .order_by(models.BankAccount.name)
        .all()
    )
    return templates.TemplateResponse(
        "expenses/form.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "expense": expense, "categories": categories, "methods": PAYMENT_METHODS,
            "paid_from_accounts": paid_from_accounts, "paid_from_methods": PAID_FROM_METHODS,
            "today": date.today().isoformat(), "error": error,
        },
    )


@router.get("/expenses/new", response_class=HTMLResponse)
def new_expense(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    return _render_form(request, db, user)


@router.get("/expenses/{expense_id:int}/edit", response_class=HTMLResponse)
def edit_expense(expense_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    expense = db.get(models.Expense, expense_id)
    if not expense:
        return RedirectResponse("/expenses", status_code=302)
    return _render_form(request, db, user, expense=expense)


@router.get("/expenses/{expense_id:int}/attachment")
def download_attachment(expense_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    expense = db.get(models.Expense, expense_id)
    if not expense or not expense.attachment_path:
        return RedirectResponse("/expenses", status_code=302)
    path = UPLOADS_DIR / expense.attachment_path
    if not path.is_file():
        return RedirectResponse("/expenses", status_code=302)
    return FileResponse(path, filename=expense.attachment_path.split("_", 1)[-1])


async def _save_attachment(form):
    """Saves an uploaded receipt/invoice file to UPLOADS_DIR and returns the
    path to store (relative to UPLOADS_DIR), or None if no file was chosen —
    an edit form re-submitting without picking a new file shouldn't clear
    the existing attachment."""
    file = form.get("attachment")
    if not file or not getattr(file, "filename", ""):
        return None
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", file.filename)[:100]
    dest_name = f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{safe_name}"
    content = await file.read()
    (UPLOADS_DIR / dest_name).write_bytes(content)
    return dest_name


def _apply_form(expense: models.Expense, db: Session, form):
    expense.category = _get_or_create_category(db, form.get("category"))
    expense.payee = (form.get("payee") or "").strip() or None
    expense.description = (form.get("description") or "").strip() or None
    expense.amount = _dec(form.get("amount"))
    expense.vat_amount = _dec(form.get("vat_amount"))
    raw_date = (form.get("expense_date") or "").strip()
    expense.expense_date = _parse_date(raw_date) or date.today()
    method = (form.get("payment_method") or "cash").strip().lower()
    expense.payment_method = method if method in dict(PAYMENT_METHODS) else "cash"
    paid_from_account_id = (form.get("paid_from_account_id") or "").strip()
    expense.paid_from_account_id = int(paid_from_account_id) if (paid_from_account_id and expense.payment_method in PAID_FROM_METHODS) else None
    expense.reference_no = (form.get("reference_no") or "").strip() or None
    expense.receipt_no = (form.get("receipt_no") or "").strip() or None
    expense.notes = (form.get("notes") or "").strip() or None


@router.post("/expenses")
async def create_expense(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    form = await request.form()
    if _dec(form.get("amount")) <= 0:
        return _render_form(request, db, user, error="Enter an amount greater than zero.")
    expense = models.Expense(created_by=user.id)
    _apply_form(expense, db, form)
    attachment = await _save_attachment(form)
    if attachment:
        expense.attachment_path = attachment
    db.add(expense)
    db.flush()
    expense.ref_no = f"EXP-{expense.id:06d}"
    try:
        accounting.post_expense(db, expense, entered_by_id=user.id)
    except accounting.PostingError:
        pass
    audit.record(
        db, user=user, request=request, action="create", entity_type="expense",
        entity_id=expense.id, entity_label=expense.ref_no,
        summary=f"Recorded expense {expense.ref_no} — {expense.amount} to {expense.payee or 'payee'}",
    )
    db.commit()
    return RedirectResponse("/expenses", status_code=status.HTTP_302_FOUND)


@router.post("/expenses/{expense_id:int}")
# TODO(accounting): editing an expense after it posted doesn't repost a
# correction — same known gap as pos.py's edit_sale_items.
async def update_expense(expense_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    expense = db.get(models.Expense, expense_id)
    if not expense:
        return RedirectResponse("/expenses", status_code=302)
    form = await request.form()
    if _dec(form.get("amount")) <= 0:
        return _render_form(request, db, user, expense=expense, error="Enter an amount greater than zero.")
    before = audit.snapshot(expense, ["amount", "payee", "description", "expense_date", "payment_method", "reference_no"])
    _apply_form(expense, db, form)
    attachment = await _save_attachment(form)
    if attachment:
        expense.attachment_path = attachment
    db.flush()
    after = audit.snapshot(expense, ["amount", "payee", "description", "expense_date", "payment_method", "reference_no"])
    changes = audit.diff(before, after)
    if changes:
        audit.record(
            db, user=user, request=request, action="update", entity_type="expense",
            entity_id=expense.id, entity_label=expense.ref_no,
            summary=f"Edited expense {expense.ref_no}", changes=changes,
        )
    db.commit()
    return RedirectResponse("/expenses", status_code=status.HTTP_302_FOUND)


@router.post("/expenses/{expense_id:int}/void")
def void_expense(expense_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    expense = db.get(models.Expense, expense_id)
    if expense:
        expense.is_voided = True
        accounting.reverse_expense_posting(db, expense, reason=f"Voided {expense.ref_no}", entered_by_id=user.id)
        audit.record(
            db, user=user, request=request, action="void", entity_type="expense",
            entity_id=expense.id, entity_label=expense.ref_no,
            summary=f"Voided expense {expense.ref_no} ({expense.amount})",
        )
        db.commit()
    return RedirectResponse("/expenses", status_code=status.HTTP_302_FOUND)
