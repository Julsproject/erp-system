"""Cash & Banking: bank accounts and their deposit/withdrawal ledger.

A balance is never stored — it's opening_balance plus the sum of that
account's transactions, computed on the fly (same idea as how a sale's
outstanding credit is derived, not cached). Moving money between two
accounts is just a withdrawal on one and a deposit on the other; there's
no separate "transfer" record type.
"""
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from . import audit, models
from .database import get_db
from .deps import get_current_user, is_admin
from .templating import templates

router = APIRouter()

PAGE_SIZE = 20
TXN_LABELS = {"deposit": "Deposit", "withdrawal": "Withdrawal"}
ZERO = Decimal("0")


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


def _balances_for(db: Session, account_ids=None):
    """{account_id: (deposits_total, withdrawals_total)} for non-voided txns."""
    q = db.query(
        models.BankTransaction.account_id,
        func.coalesce(func.sum(case((models.BankTransaction.txn_type == "deposit", models.BankTransaction.amount), else_=0)), 0),
        func.coalesce(func.sum(case((models.BankTransaction.txn_type == "withdrawal", models.BankTransaction.amount), else_=0)), 0),
    ).filter(models.BankTransaction.is_voided.is_(False))
    if account_ids is not None:
        q = q.filter(models.BankTransaction.account_id.in_(account_ids))
    rows = q.group_by(models.BankTransaction.account_id).all()
    return {r[0]: (Decimal(str(r[1] or 0)), Decimal(str(r[2] or 0))) for r in rows}


def _account_balance(account, balances: dict) -> Decimal:
    dep, wd = balances.get(account.id, (ZERO, ZERO))
    return Decimal(str(account.opening_balance or 0)) + dep - wd


@router.get("/banking", response_class=HTMLResponse)
def list_accounts(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)

    accounts = db.query(models.BankAccount).filter(models.BankAccount.is_active.is_(True)).order_by(models.BankAccount.name).all()
    balances = _balances_for(db)
    rows = [{"account": a, "balance": _account_balance(a, balances)} for a in accounts]
    total_balance = sum((r["balance"] for r in rows), ZERO)

    return templates.TemplateResponse(
        "banking/accounts.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "rows": rows, "total_balance": total_balance,
        },
    )


def _render_account_form(request, user, account=None, error=None):
    return templates.TemplateResponse(
        "banking/account_form.html",
        {"request": request, "app_name": request.app.title, "user": user, "account": account, "error": error},
    )


@router.get("/banking/accounts/new", response_class=HTMLResponse)
def new_account(request: Request, user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    return _render_account_form(request, user)


@router.post("/banking/accounts")
async def create_account(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return _render_account_form(request, user, error="Account name is required.")
    if db.query(models.BankAccount).filter(func.lower(models.BankAccount.name) == name.lower()).first():
        return _render_account_form(request, user, error=f"An account named '{name}' already exists.")
    account = models.BankAccount(
        name=name,
        bank_name=(form.get("bank_name") or "").strip() or None,
        account_no=(form.get("account_no") or "").strip() or None,
        opening_balance=_dec(form.get("opening_balance")),
    )
    db.add(account)
    db.flush()
    audit.record(
        db, user=user, request=request, action="create", entity_type="bank_account",
        entity_id=account.id, entity_label=account.name,
        summary=f"Added bank account “{account.name}” (opening {account.opening_balance})",
    )
    db.commit()
    return RedirectResponse("/banking", status_code=status.HTTP_302_FOUND)


@router.get("/banking/accounts/{account_id:int}/edit", response_class=HTMLResponse)
def edit_account(account_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    account = db.get(models.BankAccount, account_id)
    if not account:
        return RedirectResponse("/banking", status_code=302)
    return _render_account_form(request, user, account=account)


@router.post("/banking/accounts/{account_id:int}")
async def update_account(account_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    account = db.get(models.BankAccount, account_id)
    if not account:
        return RedirectResponse("/banking", status_code=302)
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return _render_account_form(request, user, account=account, error="Account name is required.")
    clash = db.query(models.BankAccount).filter(func.lower(models.BankAccount.name) == name.lower(), models.BankAccount.id != account.id).first()
    if clash:
        return _render_account_form(request, user, account=account, error=f"An account named '{name}' already exists.")
    before = audit.snapshot(account, ["name", "bank_name", "account_no", "opening_balance", "is_active"])
    account.name = name
    account.bank_name = (form.get("bank_name") or "").strip() or None
    account.account_no = (form.get("account_no") or "").strip() or None
    account.opening_balance = _dec(form.get("opening_balance"))
    account.is_active = (form.get("status") or "active") == "active"
    db.flush()
    after = audit.snapshot(account, ["name", "bank_name", "account_no", "opening_balance", "is_active"])
    changes = audit.diff(before, after)
    if changes:
        audit.record(
            db, user=user, request=request, action="update", entity_type="bank_account",
            entity_id=account.id, entity_label=account.name,
            summary=f"Edited bank account “{account.name}”", changes=changes,
        )
    db.commit()
    return RedirectResponse("/banking", status_code=status.HTTP_302_FOUND)


@router.get("/banking/accounts/{account_id:int}", response_class=HTMLResponse)
def view_account(
    account_id: int,
    request: Request,
    txn_type: str = "",
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
    account = db.get(models.BankAccount, account_id)
    if not account:
        return RedirectResponse("/banking", status_code=302)

    page = max(page, 1)
    df, dt = _parse_date(date_from), _parse_date(date_to)

    query = db.query(models.BankTransaction).filter(
        models.BankTransaction.account_id == account_id,
        models.BankTransaction.is_voided.is_(False),
    )
    if txn_type in TXN_LABELS:
        query = query.filter(models.BankTransaction.txn_type == txn_type)
    if df:
        query = query.filter(models.BankTransaction.txn_date >= df)
    if dt:
        query = query.filter(models.BankTransaction.txn_date <= dt)

    total = query.count()
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, pages)
    txns = (
        query.order_by(models.BankTransaction.txn_date.desc(), models.BankTransaction.id.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
        .all()
    )

    balances = _balances_for(db, [account_id])
    balance = _account_balance(account, balances)
    dep_total, wd_total = balances.get(account_id, (ZERO, ZERO))

    return templates.TemplateResponse(
        "banking/account_view.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "account": account, "balance": balance, "dep_total": dep_total, "wd_total": wd_total,
            "txns": txns, "txn_type": txn_type, "date_from": date_from, "date_to": date_to,
            "labels": TXN_LABELS, "page": page, "pages": pages, "total": total,
        },
    )


@router.get("/banking/accounts/{account_id:int}/transactions/new", response_class=HTMLResponse)
def new_transaction(
    account_id: int,
    request: Request,
    txn_type: str = "deposit",
    error: str = "",
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    account = db.get(models.BankAccount, account_id)
    if not account:
        return RedirectResponse("/banking", status_code=302)
    if txn_type not in TXN_LABELS:
        txn_type = "deposit"
    return templates.TemplateResponse(
        "banking/transaction_form.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "account": account, "txn_type": txn_type, "today": date.today().isoformat(), "error": error,
        },
    )


@router.post("/banking/accounts/{account_id:int}/transactions")
async def create_transaction(account_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    account = db.get(models.BankAccount, account_id)
    if not account:
        return RedirectResponse("/banking", status_code=302)
    form = await request.form()
    txn_type = (form.get("txn_type") or "").strip().lower()
    if txn_type not in TXN_LABELS:
        txn_type = "deposit"
    amount = _dec(form.get("amount"))
    if amount <= 0:
        return RedirectResponse(
            f"/banking/accounts/{account_id}/transactions/new?txn_type={txn_type}&error=Enter+an+amount+greater+than+zero.",
            status_code=302,
        )
    txn = models.BankTransaction(
        account_id=account.id,
        txn_type=txn_type,
        amount=amount,
        txn_date=_parse_date((form.get("txn_date") or "").strip()) or date.today(),
        description=(form.get("description") or "").strip() or None,
        reference_no=(form.get("reference_no") or "").strip() or None,
        created_by=user.id,
    )
    db.add(txn)
    db.flush()
    audit.record(
        db, user=user, request=request, action="create", entity_type="bank_transaction",
        entity_id=txn.id, entity_label=account.name,
        summary=f"{TXN_LABELS[txn_type]} of {amount} on “{account.name}”",
    )
    db.commit()
    return RedirectResponse(f"/banking/accounts/{account_id}", status_code=status.HTTP_302_FOUND)


@router.post("/banking/transactions/{txn_id:int}/void")
def void_transaction(txn_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    txn = db.get(models.BankTransaction, txn_id)
    if not txn:
        return RedirectResponse("/banking", status_code=302)
    account_id = txn.account_id
    txn.is_voided = True
    audit.record(
        db, user=user, request=request, action="void", entity_type="bank_transaction",
        entity_id=txn.id, entity_label=(txn.account.name if txn.account else None),
        summary=f"Voided {TXN_LABELS.get(txn.txn_type, txn.txn_type)} of {txn.amount}",
    )
    db.commit()
    return RedirectResponse(f"/banking/accounts/{account_id}", status_code=status.HTTP_302_FOUND)
