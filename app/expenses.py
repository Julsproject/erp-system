"""Expenses: business costs that aren't inventory purchases — rent, utilities,
salaries, and so on. Unlike Purchases, an expense has no pending/confirmed
staging: recording one here means the money is already out the door.
"""
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from . import models
from .database import get_db
from .deps import get_current_user, is_admin
from .templating import templates

router = APIRouter()

PAGE_SIZE = 20
PAYMENT_METHODS = [("cash", "Cash"), ("gcash", "GCash"), ("bank_transfer", "Bank Transfer"), ("cheque", "Cheque")]


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
    if not is_admin(user):
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
    return templates.TemplateResponse(
        "expenses/form.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "expense": expense, "categories": categories, "methods": PAYMENT_METHODS,
            "today": date.today().isoformat(), "error": error,
        },
    )


@router.get("/expenses/new", response_class=HTMLResponse)
def new_expense(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    return _render_form(request, db, user)


@router.get("/expenses/{expense_id:int}/edit", response_class=HTMLResponse)
def edit_expense(expense_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    expense = db.get(models.Expense, expense_id)
    if not expense:
        return RedirectResponse("/expenses", status_code=302)
    return _render_form(request, db, user, expense=expense)


def _apply_form(expense: models.Expense, db: Session, form):
    expense.category = _get_or_create_category(db, form.get("category"))
    expense.payee = (form.get("payee") or "").strip() or None
    expense.description = (form.get("description") or "").strip() or None
    expense.amount = _dec(form.get("amount"))
    raw_date = (form.get("expense_date") or "").strip()
    expense.expense_date = _parse_date(raw_date) or date.today()
    method = (form.get("payment_method") or "cash").strip().lower()
    expense.payment_method = method if method in dict(PAYMENT_METHODS) else "cash"
    expense.reference_no = (form.get("reference_no") or "").strip() or None
    expense.notes = (form.get("notes") or "").strip() or None


@router.post("/expenses")
async def create_expense(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    form = await request.form()
    if _dec(form.get("amount")) <= 0:
        return _render_form(request, db, user, error="Enter an amount greater than zero.")
    expense = models.Expense(created_by=user.id)
    _apply_form(expense, db, form)
    db.add(expense)
    db.flush()
    expense.ref_no = f"EXP-{expense.id:06d}"
    db.commit()
    return RedirectResponse("/expenses", status_code=status.HTTP_302_FOUND)


@router.post("/expenses/{expense_id:int}")
async def update_expense(expense_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    expense = db.get(models.Expense, expense_id)
    if not expense:
        return RedirectResponse("/expenses", status_code=302)
    form = await request.form()
    if _dec(form.get("amount")) <= 0:
        return _render_form(request, db, user, expense=expense, error="Enter an amount greater than zero.")
    _apply_form(expense, db, form)
    db.commit()
    return RedirectResponse("/expenses", status_code=status.HTTP_302_FOUND)


@router.post("/expenses/{expense_id:int}/void")
def void_expense(expense_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    expense = db.get(models.Expense, expense_id)
    if expense:
        expense.is_voided = True
        db.commit()
    return RedirectResponse("/expenses", status_code=status.HTTP_302_FOUND)
