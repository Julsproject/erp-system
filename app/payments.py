"""Supplier Payments — pay several invoices of one supplier with one cheque.

The front door for paying suppliers. Its whole reason to exist is that a
physical cheque is ONE piece of paper covering however many deliveries you
choose, which the per-purchase Pay button (purchases.settle_purchase_pay)
can't express and Pay Full (suppliers.supplier_pay_full_submit) can only do
all-or-nothing, at full value, on every outstanding invoice.

Here you tick the invoices, set an amount on each (prefilled with its full
balance, overwrite for a partial), and the running total IS the cheque
amount. One cheque, one supplier — the register is keyed that way on
purpose; see cheque_books.

Cheque payments don't settle anything yet: they go into the cheque register
and only become PurchaseSettlements when the bank honors them (pdc.clear_pdc).
Cash and the other methods settle immediately, on the date actually paid.
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import accounting, audit, cheque_books, models
from .database import get_db
from .deps import get_current_user, is_staff
from .purchases import PAYMENT_METHODS, _purchase_outstanding, _settled_for_purchases
from .sales import _resolve_settlement_datetime
from .suppliers import _outstanding_purchases
from .templating import templates

router = APIRouter()

MANILA = ZoneInfo("Asia/Manila")
ZERO = Decimal("0")
CENTS = Decimal("0.01")


def _today() -> date:
    return datetime.now(MANILA).date()


def _money(value) -> Decimal:
    try:
        return Decimal(str(value).strip().replace(",", "") or "0").quantize(CENTS)
    except (InvalidOperation, AttributeError, ValueError):
        return ZERO


# --------------------------------------------------------------------------- #
# Landing: who we owe, pick one to pay
# --------------------------------------------------------------------------- #
@router.get("/payments", response_class=HTMLResponse)
def payments_home(request: Request, q: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Every supplier with something outstanding, biggest first — the
    worklist you actually pay from. Deliberately not a list of payments
    already made: those live in the cheque register (/pdc) and each
    supplier's own history."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)

    q = (q or "").strip()
    settled_sub = (
        db.query(
            models.PurchaseSettlement.purchase_id.label("pid"),
            func.coalesce(func.sum(models.PurchaseSettlement.amount), 0).label("paid"),
        )
        .group_by(models.PurchaseSettlement.purchase_id)
        .subquery()
    )
    outstanding_expr = models.Purchase.total - func.coalesce(settled_sub.c.paid, 0)
    query = (
        db.query(
            models.Supplier,
            func.sum(outstanding_expr).label("owed"),
            func.count(models.Purchase.id).label("invoices"),
            func.min(models.Purchase.due_date).label("earliest_due"),
        )
        .join(models.Purchase, models.Purchase.supplier_id == models.Supplier.id)
        .outerjoin(settled_sub, settled_sub.c.pid == models.Purchase.id)
        .filter(models.Purchase.txn_type == "receive", models.Purchase.status == "confirmed")
        .filter(outstanding_expr > 0)
    )
    if q:
        query = query.filter(models.Supplier.name.ilike("%{}%".format(q)))
    rows = [
        {"supplier": s, "owed": Decimal(str(owed or 0)), "invoices": n, "earliest_due": due}
        for s, owed, n, due in query.group_by(models.Supplier.id).all()
    ]
    rows.sort(key=lambda r: r["owed"], reverse=True)
    total_owed = sum((r["owed"] for r in rows), ZERO)

    return templates.TemplateResponse(
        "payments/home.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "rows": rows, "total_owed": total_owed, "q": q, "today": _today()},
    )


# --------------------------------------------------------------------------- #
# The payment screen
# --------------------------------------------------------------------------- #
def _cheque_context(db: Session):
    """Bank accounts that can draw cheques, each with its open booklets and
    the next number on each — so the form can prefill without a round trip."""
    accounts = (
        db.query(models.BankAccount)
        .filter(models.BankAccount.is_active.is_(True))
        .order_by(models.BankAccount.name).all()
    )
    out = []
    for acct in accounts:
        books = []
        for book in cheque_books.active_books_for(db, acct.id):
            nxt = cheque_books.next_seq(db, book)
            books.append({
                "id": book.id,
                "label": "{}–{}".format(cheque_books.format_cheque_no(book, book.start_no),
                                        cheque_books.format_cheque_no(book, book.end_no)),
                "next_seq": nxt,
                "next_no": cheque_books.format_cheque_no(book, nxt) if nxt else "",
                "exhausted": nxt is None,
            })
        out.append({"id": acct.id, "name": acct.name, "books": books})
    return out


@router.get("/payments/new", response_class=HTMLResponse)
def new_payment(
    request: Request, supplier_id: int = 0, error: str = "",
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    supplier = db.get(models.Supplier, supplier_id) if supplier_id else None
    if not supplier:
        return RedirectResponse("/payments", status_code=302)

    owed = _outstanding_purchases(db, supplier.id)
    total = sum((o for _, o in owed), ZERO)
    return templates.TemplateResponse(
        "payments/new.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "supplier": supplier, "owed": owed, "total": total,
         "methods": PAYMENT_METHODS, "accounts": _cheque_context(db),
         "today": _today().isoformat(), "error": error},
    )


@router.post("/payments/new")
def create_payment(
    request: Request,
    supplier_id: str = Form("0"), method: str = Form("cheque"),
    payment_date: str = Form(""),
    bank_account_id: str = Form(""), cheque_book_id: str = Form(""),
    cheque_no: str = Form(""), cheque_date: str = Form(""),
    notes: str = Form(""),
    apply_id: list[str] = Form([]), apply_amount: list[str] = Form([]),
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)

    try:
        supplier = db.get(models.Supplier, int(supplier_id or 0))
    except ValueError:
        supplier = None
    if not supplier:
        return RedirectResponse("/payments", status_code=302)

    def fail(msg):
        return RedirectResponse(
            "/payments/new?supplier_id={}&error={}".format(supplier.id, quote(msg)),
            status_code=status.HTTP_302_FOUND,
        )

    method = (method or "cheque").strip().lower()
    if method not in dict(PAYMENT_METHODS):
        method = "cheque"

    # --- what's being paid, and is it actually still owed ------------------
    owed_map = {p.id: out for p, out in _outstanding_purchases(db, supplier.id)}
    applications = []
    total = ZERO
    for raw_id, raw_amount in zip(apply_id, apply_amount):
        try:
            pid = int(str(raw_id).strip())
        except ValueError:
            continue
        amount = _money(raw_amount)
        if amount <= 0:
            continue
        outstanding = owed_map.get(pid)
        if outstanding is None:
            # Paid off in another tab while this form sat open.
            return fail("One of those invoices is no longer outstanding — reopen the screen and try again.")
        if amount > outstanding:
            amount = outstanding   # never pay more than owed, same as the per-invoice Pay
        applications.append((pid, amount))
        total += amount

    if not applications:
        return fail("Tick at least one invoice and put an amount on it.")

    # --- cheque: goes to the register, settles nothing yet -----------------
    if method == "cheque":
        try:
            account = db.get(models.BankAccount, int(bank_account_id or 0))
        except ValueError:
            account = None
        if not account:
            return fail("Choose which bank account the cheque is drawn on.")

        try:
            cheque_date_val = date.fromisoformat((cheque_date or "").strip())
        except ValueError:
            return fail("Enter a valid cheque date (the date printed on the cheque).")

        book = None
        seq = None
        raw_book_id = (cheque_book_id or "").strip()
        if raw_book_id:
            book = db.get(models.ChequeBook, int(raw_book_id))
            if not book or book.bank_account_id != account.id:
                return fail("That cheque booklet isn't on the account you picked.")

        number_text = (cheque_no or "").strip()
        if book is not None:
            # The number must be inside the booklet and not already spoken
            # for — this is the duplicate guard. Digits are taken off the
            # typed number so a prefix ("A0012345") still resolves.
            digits = "".join(ch for ch in number_text if ch.isdigit())
            if not digits:
                return fail("Enter the cheque number.")
            seq = int(digits)
            if not (int(book.start_no) <= seq <= int(book.end_no)):
                return fail("Cheque {} isn't in booklet {}-{}.".format(
                    seq, book.start_no, book.end_no))
            if cheque_books.seq_taken(db, book.id, seq):
                return fail("Cheque {} has already been issued — check the number.".format(
                    cheque_books.format_cheque_no(book, seq)))
            number_text = cheque_books.format_cheque_no(book, seq)
        elif not number_text:
            return fail("Enter the cheque number.")

        pdc = models.PostDatedCheque(
            direction="issued", status="pending", amount=total,
            bank=account.bank_name or account.name,
            cheque_no=number_text, cheque_date=cheque_date_val,
            bank_account_id=account.id,
            cheque_book_id=book.id if book else None, cheque_seq=seq,
            supplier_id=supplier.id,
            notes=(notes or "").strip() or None,
            created_by=user.id,
        )
        db.add(pdc)
        db.flush()
        for pid, amount in applications:
            db.add(models.PdcApplication(pdc_id=pdc.id, purchase_id=pid, amount=amount))
        audit.record(
            db, user=user, request=request, action="create", entity_type="post_dated_cheque",
            entity_id=pdc.id, entity_label=pdc.cheque_no,
            summary="Issued cheque {} to {} for {} invoice(s) — {}".format(
                pdc.cheque_no, supplier.name, len(applications), total),
        )
        db.commit()
        return RedirectResponse("/pdc/{}".format(pdc.id), status_code=status.HTTP_302_FOUND)

    # --- everything else settles right away --------------------------------
    payment_dt, date_err = _resolve_settlement_datetime(payment_date)
    if date_err:
        return fail(date_err)

    for pid, amount in applications:
        purchase = db.get(models.Purchase, pid)
        if not purchase:
            continue
        settlement = models.PurchaseSettlement(
            purchase_id=purchase.id, method=method, amount=amount, created_by=user.id,
        )
        if payment_dt:
            settlement.created_at = payment_dt
        db.add(settlement)
        try:
            accounting.post_purchase_settlement(
                db, purchase, amount=amount, method=method, entered_by_id=user.id,
                txn_date=payment_dt.date() if payment_dt else None,
            )
        except accounting.PostingError:
            pass
        if amount >= owed_map[pid]:
            purchase.status = "paid"
            purchase.payment_method = method
            # The day it was actually paid, not the day it was typed in.
            purchase.paid_at = payment_dt or func.now()

    audit.record(
        db, user=user, request=request, action="create", entity_type="supplier_payment",
        entity_id=supplier.id, entity_label=supplier.name,
        summary="Paid {} — {} via {} across {} invoice(s)".format(
            supplier.name, total, dict(PAYMENT_METHODS)[method], len(applications)),
    )
    db.commit()
    return RedirectResponse("/suppliers/{}/history".format(supplier.id), status_code=status.HTTP_302_FOUND)
