"""Accounting: Chart of Accounts, the posting engine, and the reports that
read off it (General Ledger, Trial Balance).

Phase 1 only auto-posts Sales (see pos.py's _finalize_sale / void_sale).
Purchases, Expenses, and Banking are deliberately not wired yet — see the
accounting module plan for why, and the order they're meant to land in.
"""
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models
from .database import get_db
from .deps import get_current_user, is_staff
from .templating import templates

router = APIRouter()

MANILA = ZoneInfo("Asia/Manila")
CENTS = Decimal("0.01")
ZERO = Decimal("0")

ACCOUNT_TYPES = [
    ("asset", "Asset"), ("liability", "Liability"), ("equity", "Equity"),
    ("revenue", "Revenue"), ("cost_of_sales", "Cost of Sales"), ("expense", "Expense"),
]


def _dec(value, default="0") -> Decimal:
    try:
        return Decimal(str(value).strip().replace(",", "") or default)
    except (InvalidOperation, AttributeError, ValueError):
        return Decimal(default)


def _money(value) -> Decimal:
    return _dec(value).quantize(CENTS, rounding=ROUND_HALF_UP)


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
    """Same custom-range-overrides-preset logic used across every other
    report module — duplicated per this codebase's convention rather than
    imported, so each module can evolve its own filters independently."""
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


# --------------------------------------------------------------------------- #
# Posting engine
# --------------------------------------------------------------------------- #
class PostingError(Exception):
    pass


def _resolve_mapping(db: Session, function_key: str) -> models.Account:
    mapping = db.query(models.AccountMapping).filter(models.AccountMapping.function_key == function_key).first()
    if not mapping:
        raise PostingError(f"No account mapped for “{function_key}”. Set it up in Accounting Setup first.")
    if not mapping.account.is_active:
        raise PostingError(f"The account mapped to “{function_key}” ({mapping.account.name}) is inactive.")
    return mapping.account


def _next_journal_no(db: Session) -> str:
    # Row-lock the table via a real row (the max existing entry) so two
    # concurrent checkouts on different terminals can't both compute the
    # same next number — same discipline _finalize_sale already uses for
    # stock via with_for_update.
    last = (
        db.query(models.JournalEntry)
        .order_by(models.JournalEntry.id.desc())
        .with_for_update()
        .first()
    )
    next_n = (last.id + 1) if last else 1
    return f"JE-{next_n:06d}"


def post_journal(db: Session, *, txn_date, source_type: str, source_id, description: str,
                  lines: list, reference_no: str = None, entered_by_id: int = None) -> models.JournalEntry:
    """lines: [{"function_key": "SALE_CASH", "amount": Decimal, "side": "debit"|"credit", "memo": "..."}, ...]
    Does NOT commit — caller commits once, so the source transaction and its
    journal entry land or roll back together."""
    resolved = []
    total_debit = ZERO
    total_credit = ZERO
    for ln in lines:
        amount = _money(ln["amount"])
        if amount <= 0:
            continue
        account = _resolve_mapping(db, ln["function_key"])
        debit = amount if ln["side"] == "debit" else ZERO
        credit = amount if ln["side"] == "credit" else ZERO
        total_debit += debit
        total_credit += credit
        resolved.append({"account_id": account.id, "debit": debit, "credit": credit, "memo": ln.get("memo")})

    if not resolved:
        raise PostingError("Nothing to post — every line was zero.")
    if total_debit != total_credit:
        raise PostingError(f"Journal entry doesn't balance: debit {total_debit} vs credit {total_credit}.")

    entry = models.JournalEntry(
        journal_no=_next_journal_no(db), txn_date=txn_date, reference_no=reference_no,
        source_type=source_type, source_id=source_id, description=description,
        entered_by_id=entered_by_id,
    )
    db.add(entry)
    db.flush()  # need entry.id for the lines
    for ln in resolved:
        db.add(models.JournalLine(entry_id=entry.id, **ln))
    return entry


def reverse_journal(db: Session, entry: models.JournalEntry, *, reason: str = None, entered_by_id: int = None) -> models.JournalEntry:
    """Insert a mirror-image entry (every debit becomes a credit and vice
    versa) and mark the original reversed. Never mutates or deletes the
    original entry's lines."""
    reversal = models.JournalEntry(
        journal_no=_next_journal_no(db), txn_date=_today(), reference_no=entry.reference_no,
        source_type="reversal", source_id=entry.id,
        description=f"Reversal of {entry.journal_no}" + (f": {reason}" if reason else ""),
        is_reversal_of_id=entry.id, entered_by_id=entered_by_id,
    )
    db.add(reversal)
    db.flush()
    for line in entry.lines:
        db.add(models.JournalLine(
            entry_id=reversal.id, account_id=line.account_id,
            debit=line.credit, credit=line.debit, memo=line.memo,
        ))
    entry.status = "reversed"
    return reversal


def post_sale(db: Session, sale: models.Sale, *, method_rows: list, receivable_amount: Decimal, entered_by_id: int = None):
    """Called from pos.py's _finalize_sale right after sale.id is flushed.
    Posts revenue + VAT + whatever the customer actually paid with — no
    COGS/Inventory leg yet (see the accounting plan: that waits on Purchases
    posting, which is what actually builds up the Inventory account)."""
    SALE_FUNCTION_KEYS = {
        "cash": "SALE_CASH", "gcash": "SALE_GCASH", "card": "SALE_CARD", "bank_transfer": "SALE_BANK_TRANSFER",
    }
    lines = []
    for method, amount in method_rows:
        if method in ("receivable", "cheque"):
            continue  # folded into the single AR line below instead, to avoid one JE line per split
        fkey = SALE_FUNCTION_KEYS.get(method)
        if not fkey:
            continue
        lines.append({"function_key": fkey, "amount": amount, "side": "debit", "memo": method})
    if receivable_amount and receivable_amount > 0:
        lines.append({"function_key": "AR", "amount": receivable_amount, "side": "debit", "memo": "credit/cheque"})

    net = Decimal(str(sale.net_amount or 0))
    vat = Decimal(str(sale.vat_amount or 0))
    if net > 0:
        lines.append({"function_key": "SALES_REVENUE", "amount": net, "side": "credit"})
    if vat > 0:
        lines.append({"function_key": "OUTPUT_VAT", "amount": vat, "side": "credit"})

    if not lines:
        return None
    txn_date = sale.created_at.date() if sale.created_at else _today()
    return post_journal(
        db, txn_date=txn_date, source_type="sale", source_id=sale.id,
        description=f"Sale {sale.invoice_no}", reference_no=sale.invoice_no,
        lines=lines, entered_by_id=entered_by_id,
    )


def reverse_sale_posting(db: Session, sale: models.Sale, *, reason: str, entered_by_id: int = None):
    """Called from pos.py's void_sale. No-op (not an error) if the sale
    predates Phase 1 and never got a journal entry in the first place."""
    entry = (
        db.query(models.JournalEntry)
        .filter(models.JournalEntry.source_type == "sale", models.JournalEntry.source_id == sale.id,
                models.JournalEntry.status == "posted")
        .first()
    )
    if not entry:
        return None
    return reverse_journal(db, entry, reason=reason, entered_by_id=entered_by_id)


PURCHASE_PAY_FUNCTION_KEYS = {
    "cash": "PURCHASE_PAY_CASH", "gcash": "PURCHASE_PAY_GCASH",
    "bank_transfer": "PURCHASE_PAY_BANK_TRANSFER", "cheque": "PURCHASE_PAY_CHEQUE", "other": "PURCHASE_PAY_OTHER",
}


def post_purchase_receive(db: Session, purchase: models.Purchase, *, is_payable: bool, payment_method: str = None, entered_by_id: int = None):
    """Called from purchases.py's create_purchase for a receive. Dr Inventory
    always; Cr Accounts Payable if left unpaid, otherwise Cr whichever money
    account the payment method maps to (a cheque here is treated the same
    way the operational Purchase record already treats it — settled at
    receive time, not deferred like a Sales cheque is via receivable)."""
    total = Decimal(str(purchase.total or 0))
    if total <= 0:
        return None
    credit_key = "AP" if is_payable else PURCHASE_PAY_FUNCTION_KEYS.get(payment_method, "PURCHASE_PAY_OTHER")
    lines = [
        {"function_key": "INVENTORY_MERCHANDISE", "amount": total, "side": "debit"},
        {"function_key": credit_key, "amount": total, "side": "credit", "memo": payment_method or "payable"},
    ]
    txn_date = purchase.created_at.date() if purchase.created_at else _today()
    return post_journal(
        db, txn_date=txn_date, source_type="purchase", source_id=purchase.id,
        description=f"Receive {purchase.ref_no}", reference_no=purchase.ref_no,
        lines=lines, entered_by_id=entered_by_id,
    )


def post_purchase_return(db: Session, purchase: models.Purchase, *, entered_by_id: int = None):
    """A return is posted as a straight vendor credit (reduces what's owed)
    regardless of how the original delivery was paid — the common
    small-business treatment. If the supplier actually refunds cash instead
    of issuing a credit, that needs a manual adjustment (Phase 2+'s manual
    Journal Entry UI, not yet built)."""
    total = Decimal(str(purchase.total or 0))
    if total <= 0:
        return None
    lines = [
        {"function_key": "AP", "amount": total, "side": "debit"},
        {"function_key": "INVENTORY_MERCHANDISE", "amount": total, "side": "credit"},
    ]
    txn_date = purchase.created_at.date() if purchase.created_at else _today()
    return post_journal(
        db, txn_date=txn_date, source_type="purchase", source_id=purchase.id,
        description=f"Return {purchase.ref_no}", reference_no=purchase.ref_no,
        lines=lines, entered_by_id=entered_by_id,
    )


def post_purchase_settlement(db: Session, purchase: models.Purchase, *, amount: Decimal, method: str, entered_by_id: int = None):
    """Called from purchases.py's settle_purchase_pay, only on the branch
    that actually records a PurchaseSettlement (the cheque branch defers to
    a PDC instead and posts nothing yet, same idea as Sales)."""
    credit_key = PURCHASE_PAY_FUNCTION_KEYS.get(method, "PURCHASE_PAY_OTHER")
    lines = [
        {"function_key": "AP", "amount": amount, "side": "debit"},
        {"function_key": credit_key, "amount": amount, "side": "credit", "memo": method},
    ]
    return post_journal(
        db, txn_date=_today(), source_type="purchase_settlement", source_id=purchase.id,
        description=f"Payment on {purchase.ref_no}", reference_no=purchase.ref_no,
        lines=lines, entered_by_id=entered_by_id,
    )


def reverse_purchase_posting(db: Session, purchase: models.Purchase, *, reason: str, entered_by_id: int = None):
    """Called from purchases.py's cancel_purchase. Reverses only the
    original receive/return entry (source_type="purchase") — NOT any
    PurchaseSettlement postings already made against it. Cancelling a
    purchase that's already been partially paid down is a pre-existing gap
    in the operational code too (cancel_purchase doesn't block on it), so
    this mirrors that rather than silently fixing scope beyond Phase 2."""
    entry = (
        db.query(models.JournalEntry)
        .filter(models.JournalEntry.source_type == "purchase", models.JournalEntry.source_id == purchase.id,
                models.JournalEntry.status == "posted")
        .first()
    )
    if not entry:
        return None
    return reverse_journal(db, entry, reason=reason, entered_by_id=entered_by_id)


# --------------------------------------------------------------------------- #
# Chart of Accounts
# --------------------------------------------------------------------------- #
@router.get("/accounting/coa", response_class=HTMLResponse)
def coa_list(request: Request, error: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    accounts = db.query(models.Account).order_by(models.Account.code).all()
    return templates.TemplateResponse(
        "accounting/coa.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "accounts": accounts, "account_types": ACCOUNT_TYPES, "error": error},
    )


@router.post("/accounting/coa")
def coa_create(
    code: str = Form(...), name: str = Form(...), account_type: str = Form(...),
    normal_balance: str = Form(...), parent_id: str = Form(""),
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    code = (code or "").strip()
    name = (name or "").strip()
    if not code or not name:
        return RedirectResponse("/accounting/coa?error=Code+and+name+are+required.", status_code=302)
    if db.query(models.Account).filter(models.Account.code == code).first():
        return RedirectResponse(f"/accounting/coa?error=Code+%E2%80%9C{code}%E2%80%9D+is+already+used.", status_code=302)
    account = models.Account(
        code=code, name=name, account_type=account_type, normal_balance=normal_balance,
        parent_id=int(parent_id) if parent_id else None, is_system=False,
    )
    db.add(account)
    db.commit()
    return RedirectResponse("/accounting/coa", status_code=302)


@router.post("/accounting/coa/{account_id:int}/rename")
def coa_rename(account_id: int, name: str = Form(...), db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    account = db.get(models.Account, account_id)
    name = (name or "").strip()
    if account and name:
        account.name = name
        db.commit()
    return RedirectResponse("/accounting/coa", status_code=302)


@router.post("/accounting/coa/{account_id:int}/toggle")
def coa_toggle(account_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    account = db.get(models.Account, account_id)
    if not account:
        return RedirectResponse("/accounting/coa", status_code=302)
    if account.is_active:
        if account.is_system:
            return RedirectResponse(
                f"/accounting/coa?error=%E2%80%9C{account.name}%E2%80%9D+is+a+system+account+and+can%E2%80%99t+be+deactivated.",
                status_code=302,
            )
        still_mapped = db.query(models.AccountMapping.id).filter(models.AccountMapping.account_id == account.id).first()
        if still_mapped:
            return RedirectResponse(
                f"/accounting/coa?error=%E2%80%9C{account.name}%E2%80%9D+is+still+used+in+Accounting+Setup+%E2%80%94+remap+that+first.",
                status_code=302,
            )
    account.is_active = not account.is_active
    db.commit()
    return RedirectResponse("/accounting/coa", status_code=302)


# --------------------------------------------------------------------------- #
# Accounting Setup (function -> account mapping)
# --------------------------------------------------------------------------- #
@router.get("/accounting/mappings", response_class=HTMLResponse)
def mappings_list(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    mappings = db.query(models.AccountMapping).order_by(models.AccountMapping.function_key).all()
    accounts = db.query(models.Account).filter(models.Account.is_active.is_(True)).order_by(models.Account.code).all()
    return templates.TemplateResponse(
        "accounting/mappings.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "mappings": mappings, "accounts": accounts},
    )


@router.post("/accounting/mappings/{mapping_id:int}")
def mappings_update(mapping_id: int, account_id: int = Form(...), db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    mapping = db.get(models.AccountMapping, mapping_id)
    account = db.get(models.Account, account_id)
    if mapping and account and account.is_active:
        mapping.account_id = account.id
        db.commit()
    return RedirectResponse("/accounting/mappings", status_code=302)


# --------------------------------------------------------------------------- #
# General Ledger
# --------------------------------------------------------------------------- #
def _account_balance_before(db: Session, account: models.Account, before: date) -> Decimal:
    row = (
        db.query(func.coalesce(func.sum(models.JournalLine.debit), 0), func.coalesce(func.sum(models.JournalLine.credit), 0))
        .join(models.JournalEntry, models.JournalLine.entry_id == models.JournalEntry.id)
        .filter(models.JournalLine.account_id == account.id, models.JournalEntry.txn_date < before)
        .one()
    )
    debit, credit = Decimal(str(row[0] or 0)), Decimal(str(row[1] or 0))
    return (debit - credit) if account.normal_balance == "debit" else (credit - debit)


@router.get("/accounting/general-ledger", response_class=HTMLResponse)
def general_ledger(
    request: Request, account_id: int = 0, days: int = 30, date_from: str = "", date_to: str = "",
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    period_start, period_end, custom = _resolve_period(days, date_from, date_to)
    accounts = db.query(models.Account).order_by(models.Account.code).all()
    account = db.get(models.Account, account_id) if account_id else (accounts[0] if accounts else None)

    rows = []
    opening = closing = ZERO
    if account:
        opening = _account_balance_before(db, account, period_start)
        entries = (
            db.query(models.JournalLine, models.JournalEntry)
            .join(models.JournalEntry, models.JournalLine.entry_id == models.JournalEntry.id)
            .filter(models.JournalLine.account_id == account.id,
                    models.JournalEntry.txn_date.between(period_start, period_end))
            .order_by(models.JournalEntry.txn_date, models.JournalEntry.id)
            .all()
        )
        running = opening
        for line, entry in entries:
            delta = (line.debit - line.credit) if account.normal_balance == "debit" else (line.credit - line.debit)
            running += delta
            rows.append({"entry": entry, "line": line, "balance": running})
        closing = running

    return templates.TemplateResponse(
        "accounting/general_ledger.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "accounts": accounts, "account": account, "rows": rows, "opening": opening, "closing": closing,
         "days": days, "date_from": date_from, "date_to": date_to,
         "period_start": period_start, "period_end": period_end, "custom": custom},
    )


# --------------------------------------------------------------------------- #
# Trial Balance
# --------------------------------------------------------------------------- #
def _trial_balance_rows(db: Session, as_of: date):
    accounts = db.query(models.Account).filter(models.Account.is_active.is_(True)).order_by(models.Account.code).all()
    rows = []
    total_debit = total_credit = ZERO
    for account in accounts:
        totals = (
            db.query(func.coalesce(func.sum(models.JournalLine.debit), 0), func.coalesce(func.sum(models.JournalLine.credit), 0))
            .join(models.JournalEntry, models.JournalLine.entry_id == models.JournalEntry.id)
            .filter(models.JournalLine.account_id == account.id, models.JournalEntry.txn_date <= as_of)
            .one()
        )
        debit, credit = Decimal(str(totals[0] or 0)), Decimal(str(totals[1] or 0))
        if debit == 0 and credit == 0:
            continue
        net = debit - credit
        row_debit = net if net > 0 else ZERO
        row_credit = -net if net < 0 else ZERO
        rows.append({"account": account, "debit": row_debit, "credit": row_credit})
        total_debit += row_debit
        total_credit += row_credit
    return rows, total_debit, total_credit


@router.get("/accounting/trial-balance", response_class=HTMLResponse)
def trial_balance(
    request: Request, days: int = 30, date_from: str = "", date_to: str = "",
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    period_start, period_end, custom = _resolve_period(days, date_from, date_to)
    rows, total_debit, total_credit = _trial_balance_rows(db, period_end)
    return templates.TemplateResponse(
        "accounting/trial_balance.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "rows": rows, "total_debit": total_debit, "total_credit": total_credit,
         "balanced": total_debit == total_credit,
         "days": days, "date_from": date_from, "date_to": date_to,
         "period_start": period_start, "period_end": period_end, "custom": custom},
    )


@router.get("/accounting/trial-balance/export")
def export_trial_balance(days: int = 30, date_from: str = "", date_to: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    period_start, period_end, _ = _resolve_period(days, date_from, date_to)
    rows, total_debit, total_credit = _trial_balance_rows(db, period_end)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Trial Balance"
    ws.append(["Account Code", "Account Name", "Debit", "Credit"])
    header_fill = PatternFill("solid", fgColor="1F6FEB")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
    for r in rows:
        ws.append([r["account"].code, r["account"].name, float(r["debit"]), float(r["credit"])])
    ws.append(["", "Total", float(total_debit), float(total_credit)])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    widths = [14, 32, 16, 16]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"

    import io
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"trial_balance_{period_end.isoformat()}.xlsx"
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------- #
# Reconciliation: ledger Sales total vs the existing operational Sales report
# --------------------------------------------------------------------------- #
@router.get("/accounting/reconcile-sales", response_class=HTMLResponse)
def reconcile_sales(
    request: Request, days: int = 30, date_from: str = "", date_to: str = "",
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    period_start, period_end, custom = _resolve_period(days, date_from, date_to)

    ledger_total = (
        db.query(func.coalesce(func.sum(models.JournalLine.credit), 0))
        .join(models.JournalEntry, models.JournalLine.entry_id == models.JournalEntry.id)
        .join(models.Account, models.JournalLine.account_id == models.Account.id)
        .filter(
            models.Account.system_key.in_(("SALES_REVENUE", "OUTPUT_VAT")),
            models.JournalEntry.txn_date.between(period_start, period_end),
            models.JournalEntry.status == "posted",
        )
        .scalar()
    )
    ledger_total = Decimal(str(ledger_total or 0))

    # Scoped to plain "sale" transactions only — Phase 1 doesn't post
    # refunds/exchanges, so comparing against the broader P&L revenue figure
    # (which nets those in) would show a mismatch that isn't actually a bug.
    report_total = (
        db.query(func.coalesce(func.sum(models.Sale.total), 0))
        .filter(
            models.Sale.txn_type == "sale", models.Sale.is_voided.is_(False),
            _local_date(models.Sale.created_at).between(period_start, period_end),
        )
        .scalar()
    )
    report_total = Decimal(str(report_total or 0))

    unposted = []
    if ledger_total != report_total:
        sales = (
            db.query(models.Sale)
            .filter(models.Sale.txn_type == "sale", models.Sale.is_voided.is_(False),
                    _local_date(models.Sale.created_at).between(period_start, period_end))
            .all()
        )
        posted_ids = {
            sid for (sid,) in db.query(models.JournalEntry.source_id)
            .filter(models.JournalEntry.source_type == "sale", models.JournalEntry.status == "posted",
                    models.JournalEntry.source_id.in_([s.id for s in sales]))
            .all()
        }
        unposted = [s for s in sales if s.id not in posted_ids]

    return templates.TemplateResponse(
        "accounting/reconcile_sales.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "ledger_total": ledger_total, "report_total": report_total,
         "diff": ledger_total - report_total, "unposted": unposted,
         "days": days, "date_from": date_from, "date_to": date_to,
         "period_start": period_start, "period_end": period_end, "custom": custom},
    )
