"""Post-dated cheque (PDC) register.

A PDC holds a payment in limbo until the bank actually honors it:
  received (from a customer settling credit) -> clearing creates the
    ReceivableSettlement only now, so the credit balance doesn't drop until
    the cheque is proven good.
  issued (to pay a supplier) -> clearing creates a PurchaseSettlement only
    now, for whatever the cheque was worth (it may only be a partial
    payment); the purchase flips to "paid" once that brings its balance
    to zero, not necessarily on this one cheque alone.
Bouncing a received cheque needs no reversal (nothing was ever applied);
bouncing an issued one just leaves the purchase's balance as it was.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_, text
from sqlalchemy.orm import Session

from . import accounting, audit, models
from .credits import _outstanding_sales
from .database import get_db
from .deps import get_current_user, is_staff
from .sales import _resolve_settlement_datetime
from .suppliers import _outstanding_purchases
from .templating import templates

router = APIRouter()

MANILA = ZoneInfo("Asia/Manila")
STATUS_LABELS = {"pending": "Pending", "deposited": "Deposited", "cleared": "Cleared",
                 "bounced": "Bounced", "cancelled": "Cancelled", "voided": "Voided"}
DUE_SOON_DAYS = 3
PAGE_SIZE = 15


def _today() -> date:
    return datetime.now(MANILA).date()


@router.get("/pdc", response_class=HTMLResponse)
def list_pdc(
    request: Request,
    direction: str = "",
    status_filter: str = "",
    q: str = "",
    page: int = 1,
    deleted: int = 0,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)

    page = max(page, 1)
    q = (q or "").strip()
    query = db.query(models.PostDatedCheque)
    if q:
        # Cheque number, bank, or whoever it was written to/from — the three
        # things you actually have in hand when hunting for one. Outer joins
        # so a cheque with no party attached still matches on its number.
        like = f"%{q}%"
        query = (
            query.outerjoin(models.Supplier, models.PostDatedCheque.supplier_id == models.Supplier.id)
            .outerjoin(models.Customer, models.PostDatedCheque.customer_id == models.Customer.id)
            .filter(or_(
                models.PostDatedCheque.cheque_no.ilike(like),
                models.PostDatedCheque.bank.ilike(like),
                models.PostDatedCheque.notes.ilike(like),
                models.Supplier.name.ilike(like),
                models.Customer.name.ilike(like),
            ))
        )
    if direction in ("received", "issued"):
        query = query.filter(models.PostDatedCheque.direction == direction)
    if status_filter in STATUS_LABELS:
        query = query.filter(models.PostDatedCheque.status == status_filter)
    else:
        # A voided cheque is a spoiled leaf recorded only so its NUMBER is
        # accounted for in the series register — it pays nothing and is
        # waiting for nothing, so it would only be noise here. Still
        # reachable by filtering for it, and always visible in the booklet.
        query = query.filter(models.PostDatedCheque.status != "voided")

    total = query.count()
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, pages)

    # Pending ones soonest-due first; resolved ones most-recent first.
    if status_filter == "pending" or not status_filter:
        ordered = query.order_by(
            (models.PostDatedCheque.status != "pending"),
            models.PostDatedCheque.cheque_date,
        )
    else:
        ordered = query.order_by(models.PostDatedCheque.cheque_date.desc())
    rows = ordered.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()

    today = _today()
    horizon = today + timedelta(days=DUE_SOON_DAYS)
    all_pdcs = db.query(models.PostDatedCheque).all()
    counts = {s: sum(1 for p in all_pdcs if p.status == s) for s in STATUS_LABELS}
    pending_total = sum((p.amount or Decimal("0")) for p in all_pdcs if p.status == "pending")
    # How many are sitting at or past their date, waiting for someone to
    # confirm the bank honored them — the worklist's badge.
    due_now = sum(1 for p in all_pdcs
                  if p.status in ("pending", "deposited") and p.cheque_date and p.cheque_date <= today)

    return templates.TemplateResponse(
        "pdc/list.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "rows": rows, "direction": direction, "status_filter": status_filter,
            "counts": counts, "labels": STATUS_LABELS, "pending_total": pending_total, "due_now": due_now, "q": q,
            "today": today, "horizon": horizon, "page": page, "pages": pages, "deleted": deleted,
        },
    )


@router.get("/pdc/{pdc_id:int}", response_class=HTMLResponse)
def view_pdc(pdc_id: int, request: Request, delete_error: int = 0, unclear_error: int = 0, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    pdc = db.get(models.PostDatedCheque, pdc_id)
    if not pdc:
        return RedirectResponse("/pdc", status_code=302)
    return templates.TemplateResponse(
        "pdc/view.html",
        {"request": request, "app_name": request.app.title, "user": user, "pdc": pdc, "today": _today(),
         "delete_error": delete_error, "unclear_error": unclear_error},
    )


@router.post("/pdc/{pdc_id:int}/deposit")
def deposit_pdc(pdc_id: int, request: Request, deposit_date: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Handed to the bank teller — a physical-custody marker only, no
    financial posting yet (same limbo as pending). Optional step; clear/
    bounce both still work directly from pending too."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    pdc = db.get(models.PostDatedCheque, pdc_id)
    if not pdc or pdc.status != "pending":
        return RedirectResponse(f"/pdc/{pdc_id}", status_code=302)
    try:
        pdc.deposit_date = date.fromisoformat(deposit_date) if deposit_date else _today()
    except ValueError:
        pdc.deposit_date = _today()
    pdc.status = "deposited"
    audit.record(
        db, user=user, request=request, action="deposit", entity_type="post_dated_cheque",
        entity_id=pdc.id, entity_label=pdc.cheque_no or f"PDC-{pdc.id}",
        summary=f"Marked cheque {pdc.cheque_no or pdc.id} as deposited ({pdc.bank or 'bank'}, {pdc.amount})",
    )
    db.commit()
    return RedirectResponse(f"/pdc/{pdc_id}", status_code=status.HTTP_302_FOUND)


def _apply_clearing(db: Session, pdc: models.PostDatedCheque, clear_dt, user) -> None:
    """Everything that happens when the bank honors a cheque.

    Lifted out of clear_pdc so the one-at-a-time button and the bulk
    "clear the cheques that came due" worklist run the SAME code — this
    posts settlements, flips invoices to paid and moves a bank balance,
    which is not logic to have two copies of. Caller does the status
    guard, the audit record and the commit.
    """
    applications = list(pdc.applications)
    if not applications:
        # Legacy PDC from before pdc_applications existed (shouldn't happen
        # post-migration — every row got backfilled one — but a defensive
        # fallback costs nothing).
        if pdc.sale_id:
            applications = [models.PdcApplication(pdc_id=pdc.id, sale_id=pdc.sale_id, amount=pdc.amount)]
        elif pdc.purchase_id:
            applications = [models.PdcApplication(pdc_id=pdc.id, purchase_id=pdc.purchase_id, amount=pdc.amount)]

    if pdc.direction == "received":
        last_settlement_id = None
        for app in applications:
            if not app.sale_id:
                continue
            sale = db.get(models.Sale, app.sale_id)
            if not sale:
                continue
            settlement = models.ReceivableSettlement(
                sale_id=app.sale_id, method="cheque", amount=app.amount,
                bank=pdc.bank, cheque_no=pdc.cheque_no, cheque_date=pdc.cheque_date.isoformat(),
                cashier_id=user.id, pdc_id=pdc.id,
            )
            if clear_dt:
                settlement.created_at = clear_dt
            db.add(settlement)
            db.flush()
            last_settlement_id = settlement.id
            try:
                entry = accounting.post_receivable_settlement(
                    db, sale, amount=app.amount, method="cheque", entered_by_id=user.id,
                    txn_date=clear_dt.date() if clear_dt else None,
                )
                # Remember the exact entry, so un-clearing reverses THIS
                # payment rather than guessing from source_type/source_id.
                if entry is not None:
                    settlement.journal_entry_id = entry.id
            except accounting.PostingError:
                pass
        # Informational only when a cheque covers one invoice; with several,
        # this just points at the last settlement it created.
        pdc.settlement_id = last_settlement_id
    else:
        for app in applications:
            if not app.purchase_id:
                continue
            purchase = db.get(models.Purchase, app.purchase_id)
            if not purchase:
                continue
            # A delivery saved as "paid by cheque" is already booked as paid
            # (Cr Bank on its delivery date) and already marked paid — its
            # cheque clearing only confirms that. Settling it again would
            # post the payment a second time.
            if purchase.status == "paid":
                continue
            # Same partial-payment idea as the received side: clearing posts
            # a PurchaseSettlement for this application's amount, and the
            # purchase only flips to "paid" once that brings its own balance
            # to zero — independently per purchase, since one cheque can now
            # cover several at once.
            purchase_settlement = models.PurchaseSettlement(
                purchase_id=purchase.id, method="cheque", amount=app.amount,
                bank=pdc.bank, cheque_no=pdc.cheque_no, cheque_date=pdc.cheque_date.isoformat(),
                created_by=user.id, pdc_id=pdc.id,
            )
            if clear_dt:
                purchase_settlement.created_at = clear_dt
            db.add(purchase_settlement)
            try:
                entry = accounting.post_purchase_settlement(
                    db, purchase, amount=app.amount, method="cheque", entered_by_id=user.id,
                    txn_date=clear_dt.date() if clear_dt else None,
                )
                if entry is not None:
                    purchase_settlement.journal_entry_id = entry.id
            except accounting.PostingError:
                pass
            db.flush()
            paid_so_far = (
                db.query(func.coalesce(func.sum(models.PurchaseSettlement.amount), 0))
                .filter(models.PurchaseSettlement.purchase_id == purchase.id)
                .scalar()
            )
            if Decimal(str(paid_so_far or 0)) >= (purchase.total or Decimal("0")):
                purchase.status = "paid"
                purchase.payment_method = "cheque"
                purchase.paid_at = clear_dt or func.now()

        # The money leaves the bank the day the cheque CLEARS, not the day
        # it was written — so the Cash & Banking balance only drops now.
        # Deliberately given no contra_account_id: post_purchase_settlement
        # just above already books the double entry for this same payment,
        # and a GL-posted bank transaction would count it twice. (That's
        # also why accounting.post_bank_transaction isn't called here — it
        # no-ops without a contra account anyway.) Only for cheques drawn on
        # an account we actually track; older ones have no bank_account_id
        # and simply don't move a balance, as before.
        if pdc.bank_account_id and (pdc.amount or Decimal("0")) > 0:
            payee = pdc.supplier.name if pdc.supplier else "supplier"
            withdrawal = models.BankTransaction(
                account_id=pdc.bank_account_id,
                txn_type="withdrawal",
                amount=pdc.amount,
                txn_date=clear_dt.date() if clear_dt else _today(),
                description="Cheque {} — {}".format(pdc.cheque_no or pdc.id, payee),
                reference_no=pdc.cheque_no,
                created_by=user.id,
            )
            db.add(withdrawal)
            db.flush()
            pdc.bank_txn_id = withdrawal.id

    pdc.status = "cleared"
    pdc.resolved_at = clear_dt or func.now()


def _cleared_without_settlements(db: Session, pdc: models.PostDatedCheque) -> bool:
    """An issued cheque for deliveries that were already paid when received
    clears without posting anything (see _apply_clearing), so un-clearing it
    has nothing to reverse. Told apart from a legacy trail-less clearing by
    its deliveries having no payment rows at all."""
    if pdc.direction != "issued":
        return False
    ids = [a.purchase_id for a in pdc.applications if a.purchase_id] or ([pdc.purchase_id] if pdc.purchase_id else [])
    if not ids:
        return False
    return not db.query(models.PurchaseSettlement).filter(models.PurchaseSettlement.purchase_id.in_(ids)).first()         and all((db.get(models.Purchase, i) is not None and db.get(models.Purchase, i).status == "paid") for i in ids)


AUTO_CLEAR_LOCK = 725_001   # pg advisory lock id: one auto-clear run at a time


def auto_clear_issued_cheques(db: Session) -> int:
    """Clear every issued cheque whose date has come, dated on the cheque
    date itself — our own cheques are taken as honored on their date. Run at
    startup and hourly (see main.py). Received cheques stay manual: a
    customer's cheque can still bounce. A cheque someone un-cleared by hand
    is never re-cleared automatically."""
    if not db.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": AUTO_CLEAR_LOCK}).scalar():
        return 0
    due = (db.query(models.PostDatedCheque)
           .filter(models.PostDatedCheque.direction == "issued",
                   models.PostDatedCheque.status.in_(("pending", "deposited")),
                   models.PostDatedCheque.cheque_date <= _today(),
                   # Un-cleared by hand (e.g. to mark it bounced): leave it to them.
                   ~models.PostDatedCheque.id.in_(
                       db.query(models.AuditLog.entity_id).filter(
                           models.AuditLog.entity_type == "post_dated_cheque",
                           models.AuditLog.action == "unclear",
                           models.AuditLog.entity_id.isnot(None))))
           .order_by(models.PostDatedCheque.cheque_date, models.PostDatedCheque.id)
           .with_for_update(skip_locked=True).all())
    if not due:
        db.rollback()
        return 0
    system_user = (db.query(models.User).filter(models.User.role == "admin", models.User.is_active.is_(True))
                   .order_by(models.User.id).first())
    if system_user is None:
        db.rollback()
        return 0
    now = datetime.now(MANILA)
    for pdc in due:
        clear_dt = now.replace(year=pdc.cheque_date.year, month=pdc.cheque_date.month, day=pdc.cheque_date.day)
        _apply_clearing(db, pdc, clear_dt, system_user)
        audit.record(
            db, user=system_user, action="clear", entity_type="post_dated_cheque",
            entity_id=pdc.id, entity_label=pdc.cheque_no or f"PDC-{pdc.id}",
            summary=f"Cleared cheque {pdc.cheque_no or pdc.id} — {pdc.amount} (automatic, on its cheque date {pdc.cheque_date})",
        )
    db.commit()
    return len(due)


@router.post("/pdc/{pdc_id:int}/clear")
def clear_pdc(pdc_id: int, request: Request, clear_date: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    """The bank honored it: apply the payment it represents, only now.
    `clear_date` backdates the resulting settlement (and its posting) to
    when the cheque actually cleared, for a cheque that's only being marked
    cleared in the system after the fact — defaults to now."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    pdc = db.get(models.PostDatedCheque, pdc_id)
    if not pdc or pdc.status not in ("pending", "deposited"):
        return RedirectResponse(f"/pdc/{pdc_id}", status_code=302)
    # Lenient like deposit_pdc's own date handling above — an invalid or
    # future date just falls back to live 'now' rather than blocking the
    # whole action over a date field.
    clear_dt, _ = _resolve_settlement_datetime(clear_date)

    _apply_clearing(db, pdc, clear_dt, user)
    audit.record(
        db, user=user, request=request, action="clear", entity_type="post_dated_cheque",
        entity_id=pdc.id, entity_label=pdc.cheque_no or f"PDC-{pdc.id}",
        summary=f"Cleared cheque {pdc.cheque_no or pdc.id} — {pdc.amount}",
    )
    db.commit()
    return RedirectResponse(f"/pdc/{pdc_id}", status_code=status.HTTP_302_FOUND)


@router.post("/pdc/{pdc_id:int}/bounce")
def bounce_pdc(pdc_id: int, request: Request, notes: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    """The bank rejected it. Received: nothing to undo. Issued: stays unpaid."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    pdc = db.get(models.PostDatedCheque, pdc_id)
    if not pdc or pdc.status not in ("pending", "deposited"):
        return RedirectResponse(f"/pdc/{pdc_id}", status_code=302)

    note = (notes or "").strip()
    if note:
        pdc.notes = note
    pdc.status = "bounced"
    pdc.resolved_at = func.now()
    audit.record(
        db, user=user, request=request, action="bounce", entity_type="post_dated_cheque",
        entity_id=pdc.id, entity_label=pdc.cheque_no or f"PDC-{pdc.id}",
        summary=f"Bounced cheque {pdc.cheque_no or pdc.id} — {pdc.amount}" + (f" ({note})" if note else ""),
    )
    db.commit()
    return RedirectResponse(f"/pdc/{pdc_id}", status_code=status.HTTP_302_FOUND)


@router.post("/pdc/{pdc_id:int}/cancel")
def cancel_pdc(pdc_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """The cheque was returned/replaced before ever being deposited."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    pdc = db.get(models.PostDatedCheque, pdc_id)
    if pdc and pdc.status == "pending":
        pdc.status = "cancelled"
        pdc.resolved_at = func.now()
        audit.record(
            db, user=user, request=request, action="cancel", entity_type="post_dated_cheque",
            entity_id=pdc.id, entity_label=pdc.cheque_no or f"PDC-{pdc.id}",
            summary=f"Cancelled cheque {pdc.cheque_no or pdc.id} — {pdc.amount}",
        )
        db.commit()
    return RedirectResponse(f"/pdc/{pdc_id}", status_code=status.HTTP_302_FOUND)


@router.post("/pdc/{pdc_id:int}/delete")
def delete_pdc(pdc_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Removes a pending cheque entirely — for a genuine data-entry mistake
    (e.g. the same cheque logged 2-3 times), not a real transaction worth
    keeping a "cancelled" record of. Only allowed while still pending: a
    cheque never posted anything to the books at that stage (see
    create_pdc — no journal entry, no settlement, nothing to reverse), so
    there's nothing left over once it's gone. Once deposited/cleared/bounced,
    real financial history exists against it — that has to go through
    Cancel (a soft void) instead, never a hard delete."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    pdc = db.get(models.PostDatedCheque, pdc_id)
    if not pdc:
        return RedirectResponse("/pdc", status_code=status.HTTP_302_FOUND)
    if pdc.status != "pending":
        return RedirectResponse(f"/pdc/{pdc_id}?delete_error=1", status_code=status.HTTP_302_FOUND)

    audit.record(
        db, user=user, request=request, action="delete", entity_type="post_dated_cheque",
        entity_id=pdc.id, entity_label=pdc.cheque_no or f"PDC-{pdc.id}",
        summary=f"Deleted cheque {pdc.cheque_no or pdc.id} — {pdc.amount} (data-entry mistake)",
    )
    db.delete(pdc)
    db.commit()
    return RedirectResponse("/pdc?deleted=1", status_code=status.HTTP_302_FOUND)


# --------------------------------------------------------------------------- #
# Manual multi-invoice entry — one physical cheque, several invoices at once.
# --------------------------------------------------------------------------- #
@router.get("/pdc/new", response_class=HTMLResponse)
def new_pdc(
    request: Request, direction: str = "received", q: str = "",
    customer_id: int = 0, supplier_id: int = 0, error: str = "",
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    direction = "issued" if direction == "issued" else "received"
    q = (q or "").strip()

    party = None
    owed = []
    matches = []
    if direction == "received":
        if customer_id:
            party = db.get(models.Customer, customer_id)
            if party:
                owed = _outstanding_sales(db, party.id)
        elif q:
            matches = (
                db.query(models.Customer)
                .join(models.Sale, models.Sale.customer_id == models.Customer.id)
                .filter(models.Sale.receivable_amount > 0, models.Customer.name.ilike(f"%{q}%"))
                .distinct().order_by(models.Customer.name).limit(20).all()
            )
    else:
        if supplier_id:
            party = db.get(models.Supplier, supplier_id)
            if party:
                owed = _outstanding_purchases(db, party.id)
        elif q:
            matches = (
                db.query(models.Supplier)
                .join(models.Purchase, models.Purchase.supplier_id == models.Supplier.id)
                .filter(models.Purchase.txn_type == "receive", models.Purchase.status == "confirmed",
                        models.Supplier.name.ilike(f"%{q}%"))
                .distinct().order_by(models.Supplier.name).limit(20).all()
            )

    return templates.TemplateResponse(
        "pdc/new.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "direction": direction, "q": q, "party": party, "owed": owed, "matches": matches,
         "today": _today().isoformat(), "error": error},
    )


@router.post("/pdc/new")
def create_pdc(
    request: Request,
    direction: str = Form(""), customer_id: str = Form("0"), supplier_id: str = Form("0"),
    bank: str = Form(""), cheque_no: str = Form(""), cheque_date: str = Form(""),
    apply_id: list[str] = Form([]), apply_amount: list[str] = Form([]),
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    direction = "issued" if direction == "issued" else "received"
    customer_id = int(customer_id or 0) or None
    supplier_id = int(supplier_id or 0) or None
    bank = (bank or "").strip() or None
    cheque_no = (cheque_no or "").strip() or None
    raw_date = (cheque_date or "").strip()

    def back_with_error(msg):
        qs = f"direction={direction}&customer_id={customer_id or ''}&supplier_id={supplier_id or ''}"
        return RedirectResponse(f"/pdc/new?{qs}&error={quote(msg)}", status_code=status.HTTP_302_FOUND)

    try:
        cheque_date = date.fromisoformat(raw_date)
    except ValueError:
        return back_with_error("Enter a valid cheque date (the date printed on the cheque).")

    ids = apply_id
    amounts = apply_amount
    applications = []
    total = Decimal("0")
    for raw_id, raw_amount in zip(ids, amounts):
        try:
            amount = Decimal(str(raw_amount).strip() or "0")
        except Exception:
            continue
        if amount <= 0:
            continue
        applications.append((int(raw_id), amount))
        total += amount

    if not applications:
        return back_with_error("Select at least one invoice and amount.")

    pdc = models.PostDatedCheque(
        direction=direction, amount=total, bank=bank, cheque_no=cheque_no, cheque_date=cheque_date,
        customer_id=customer_id, supplier_id=supplier_id, created_by=user.id,
    )
    db.add(pdc)
    db.flush()
    for entity_id, amount in applications:
        if direction == "received":
            db.add(models.PdcApplication(pdc_id=pdc.id, sale_id=entity_id, amount=amount))
        else:
            db.add(models.PdcApplication(pdc_id=pdc.id, purchase_id=entity_id, amount=amount))
    audit.record(
        db, user=user, request=request, action="create", entity_type="post_dated_cheque",
        entity_id=pdc.id, entity_label=pdc.cheque_no or f"PDC-{pdc.id}",
        summary=f"Recorded {direction} cheque {pdc.cheque_no or pdc.id} against {len(applications)} invoice(s) — {total}",
    )
    db.commit()
    return RedirectResponse(f"/pdc/{pdc.id}", status_code=status.HTTP_302_FOUND)


# --------------------------------------------------------------------------- #
# "Cheques that came due" worklist — clear a whole batch in one go.
# --------------------------------------------------------------------------- #
# Deliberately NOT an automatic sweep. Clearing means the bank actually
# honored the cheque: it posts settlements, marks invoices paid and (for an
# issued cheque drawn on a tracked account) takes the money out of the bank
# balance. A cheque reaching its date proves none of that — suppliers sit on
# post-dated cheques for weeks, and cheques bounce. Firing this off a
# calendar would quietly corrupt both the payables and the bank balance with
# nobody looking, and there's no un-clear to undo it with. So the date only
# decides what gets LISTED here; a person still says go.
@router.get("/pdc/due", response_class=HTMLResponse)
def due_worklist(
    request: Request, direction: str = "", cleared: int = 0,
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)

    today = _today()
    query = (
        db.query(models.PostDatedCheque)
        .filter(models.PostDatedCheque.status.in_(("pending", "deposited")),
                models.PostDatedCheque.cheque_date <= today)
    )
    if direction in ("received", "issued"):
        query = query.filter(models.PostDatedCheque.direction == direction)
    rows = query.order_by(models.PostDatedCheque.cheque_date,
                          models.PostDatedCheque.id).all()

    issued_total = sum((r.amount or Decimal("0")) for r in rows if r.direction == "issued")
    received_total = sum((r.amount or Decimal("0")) for r in rows if r.direction == "received")

    return templates.TemplateResponse(
        "pdc/due.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "rows": rows, "direction": direction, "today": today,
         "today_iso": today.isoformat(), "cleared": cleared,
         "issued_total": issued_total, "received_total": received_total,
         "grand_total": issued_total + received_total},
    )


@router.post("/pdc/due/clear")
def bulk_clear(
    request: Request, clear_date: str = Form(""), pdc_id: list[str] = Form([]),
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    """Clear every ticked cheque, all on one clearing date.

    One date for the batch, not per cheque: the realistic workflow is
    "I checked the bank statement today, these five went through". Cheques
    that cleared on different days are different batches.
    """
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)

    clear_dt, _ = _resolve_settlement_datetime(clear_date)

    done = 0
    for raw in pdc_id:
        try:
            pdc = db.get(models.PostDatedCheque, int(str(raw).strip()))
        except ValueError:
            continue
        # Re-check the status per cheque rather than trusting the form: one
        # of these may have been cleared or bounced from another screen
        # while the list sat open.
        if not pdc or pdc.status not in ("pending", "deposited"):
            continue
        _apply_clearing(db, pdc, clear_dt, user)
        # One audit row per cheque, not one for the batch — each is its own
        # financial event and has to be traceable on its own.
        audit.record(
            db, user=user, request=request, action="clear", entity_type="post_dated_cheque",
            entity_id=pdc.id, entity_label=pdc.cheque_no or f"PDC-{pdc.id}",
            summary=f"Cleared cheque {pdc.cheque_no or pdc.id} — {pdc.amount} (bulk)",
        )
        done += 1

    db.commit()
    return RedirectResponse(f"/pdc/due?cleared={done}", status_code=status.HTTP_302_FOUND)


@router.post("/pdc/{pdc_id:int}/unclear")
def unclear_pdc(pdc_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Undo a clearing that shouldn't have happened — the cheque goes back to
    pending and everything the clearing did is reversed.

    Clearing is the one irreversible thing the cheque register used to do, so
    a mis-tick on the bulk worklist had to be unpicked by hand. This puts it
    back: the settlements it created are reversed in the ledger and removed,
    any invoice it flipped to "paid" reopens, and the bank withdrawal is
    voided so the balance comes back.

    A cheque cleared BEFORE the pdc_id trail existed (migration 0070) can't
    be undone here — there's no reliable way to tell which settlements were
    its, and guessing from cheque_no would risk reversing someone else's
    payment. Those are refused rather than half-undone.
    """
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    pdc = db.get(models.PostDatedCheque, pdc_id)
    if not pdc or pdc.status != "cleared":
        return RedirectResponse(f"/pdc/{pdc_id}", status_code=302)

    label = pdc.cheque_no or f"PDC-{pdc.id}"
    reason = f"Un-cleared cheque {label}"

    if pdc.direction == "received":
        rows = db.query(models.ReceivableSettlement).filter(
            models.ReceivableSettlement.pdc_id == pdc.id).all()
    else:
        rows = db.query(models.PurchaseSettlement).filter(
            models.PurchaseSettlement.pdc_id == pdc.id).all()

    if not rows and not _cleared_without_settlements(db, pdc):
        # Legacy clearing with no trail — see the docstring.
        return RedirectResponse(f"/pdc/{pdc_id}?unclear_error=1", status_code=status.HTTP_302_FOUND)

    if pdc.direction == "received":
        for settlement in rows:
            accounting.reverse_receivable_settlement(
                db, settlement, reason=reason, entered_by_id=user.id)
            db.delete(settlement)
        pdc.settlement_id = None
    else:
        touched = []
        for settlement in rows:
            purchase = db.get(models.Purchase, settlement.purchase_id)
            accounting.reverse_purchase_settlement(
                db, settlement, reason=reason, entered_by_id=user.id)
            db.delete(settlement)
            if purchase is not None:
                touched.append(purchase)
        db.flush()
        # Reopen anything this cheque had closed — but only after the rows are
        # gone, and only if what's left really no longer covers the total: the
        # purchase may have been paid down by another cheque as well.
        for purchase in touched:
            if purchase.status != "paid":
                continue
            paid_so_far = (
                db.query(func.coalesce(func.sum(models.PurchaseSettlement.amount), 0))
                .filter(models.PurchaseSettlement.purchase_id == purchase.id)
                .scalar()
            )
            if Decimal(str(paid_so_far or 0)) < (purchase.total or Decimal("0")):
                purchase.status = "confirmed"
                purchase.paid_at = None

        # Put the money back in the bank. Voided rather than deleted, so the
        # account's history still shows it happened and was undone.
        if pdc.bank_txn_id:
            txn = db.get(models.BankTransaction, pdc.bank_txn_id)
            if txn is not None and not txn.is_voided:
                txn.is_voided = True
                accounting.reverse_bank_transaction_posting(
                    db, txn, reason=reason, entered_by_id=user.id)
            pdc.bank_txn_id = None

    # Back to limbo, exactly where it was before someone said it cleared.
    pdc.status = "pending"
    pdc.resolved_at = None
    audit.record(
        db, user=user, request=request, action="unclear", entity_type="post_dated_cheque",
        entity_id=pdc.id, entity_label=label,
        summary=f"Un-cleared cheque {label} — reversed {len(rows)} settlement(s), {pdc.amount}",
    )
    db.commit()
    return RedirectResponse(f"/pdc/{pdc_id}", status_code=status.HTTP_302_FOUND)
