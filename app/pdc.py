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

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import accounting, audit, models
from .credits import _outstanding_sales
from .database import get_db
from .deps import get_current_user, is_staff
from .suppliers import _outstanding_purchases
from .templating import templates

router = APIRouter()

MANILA = ZoneInfo("Asia/Manila")
STATUS_LABELS = {"pending": "Pending", "deposited": "Deposited", "cleared": "Cleared", "bounced": "Bounced", "cancelled": "Cancelled"}
DUE_SOON_DAYS = 3
PAGE_SIZE = 15


def _today() -> date:
    return datetime.now(MANILA).date()


@router.get("/pdc", response_class=HTMLResponse)
def list_pdc(
    request: Request,
    direction: str = "",
    status_filter: str = "",
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
    query = db.query(models.PostDatedCheque)
    if direction in ("received", "issued"):
        query = query.filter(models.PostDatedCheque.direction == direction)
    if status_filter in STATUS_LABELS:
        query = query.filter(models.PostDatedCheque.status == status_filter)

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

    return templates.TemplateResponse(
        "pdc/list.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "rows": rows, "direction": direction, "status_filter": status_filter,
            "counts": counts, "labels": STATUS_LABELS, "pending_total": pending_total,
            "today": today, "horizon": horizon, "page": page, "pages": pages, "deleted": deleted,
        },
    )


@router.get("/pdc/{pdc_id:int}", response_class=HTMLResponse)
def view_pdc(pdc_id: int, request: Request, delete_error: int = 0, db: Session = Depends(get_db), user=Depends(get_current_user)):
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
         "delete_error": delete_error},
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


@router.post("/pdc/{pdc_id:int}/clear")
def clear_pdc(pdc_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """The bank honored it: apply the payment it represents, only now."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    pdc = db.get(models.PostDatedCheque, pdc_id)
    if not pdc or pdc.status not in ("pending", "deposited"):
        return RedirectResponse(f"/pdc/{pdc_id}", status_code=302)

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
                cashier_id=user.id,
            )
            db.add(settlement)
            db.flush()
            last_settlement_id = settlement.id
            try:
                accounting.post_receivable_settlement(db, sale, amount=app.amount, method="cheque", entered_by_id=user.id)
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
            # Same partial-payment idea as the received side: clearing posts
            # a PurchaseSettlement for this application's amount, and the
            # purchase only flips to "paid" once that brings its own balance
            # to zero — independently per purchase, since one cheque can now
            # cover several at once.
            db.add(models.PurchaseSettlement(
                purchase_id=purchase.id, method="cheque", amount=app.amount,
                bank=pdc.bank, cheque_no=pdc.cheque_no, cheque_date=pdc.cheque_date.isoformat(),
                created_by=user.id,
            ))
            try:
                accounting.post_purchase_settlement(db, purchase, amount=app.amount, method="cheque", entered_by_id=user.id)
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
                purchase.paid_at = func.now()

    pdc.status = "cleared"
    pdc.resolved_at = func.now()
    audit.record(
        db, user=user, request=request, action="clear", entity_type="post_dated_cheque",
        entity_id=pdc.id, entity_label=pdc.cheque_no or f"PDC-{pdc.id}",
        summary=f"Cleared cheque {pdc.cheque_no or pdc.id} — {pdc.amount}",
    )
    db.commit()
    return RedirectResponse(f"/pdc/{pdc_id}", status_code=status.HTTP_302_FOUND)


@router.post("/pdc/{pdc_id:int}/bounce")
async def bounce_pdc(pdc_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """The bank rejected it. Received: nothing to undo. Issued: stays unpaid."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    pdc = db.get(models.PostDatedCheque, pdc_id)
    if not pdc or pdc.status not in ("pending", "deposited"):
        return RedirectResponse(f"/pdc/{pdc_id}", status_code=302)

    form = await request.form()
    note = (form.get("notes") or "").strip()
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
async def create_pdc(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    form = await request.form()
    direction = "issued" if form.get("direction") == "issued" else "received"
    customer_id = int(form.get("customer_id") or 0) or None
    supplier_id = int(form.get("supplier_id") or 0) or None
    bank = (form.get("bank") or "").strip() or None
    cheque_no = (form.get("cheque_no") or "").strip() or None
    raw_date = (form.get("cheque_date") or "").strip()

    def back_with_error(msg):
        qs = f"direction={direction}&customer_id={customer_id or ''}&supplier_id={supplier_id or ''}"
        return RedirectResponse(f"/pdc/new?{qs}&error={quote(msg)}", status_code=status.HTTP_302_FOUND)

    try:
        cheque_date = date.fromisoformat(raw_date)
    except ValueError:
        return back_with_error("Enter a valid cheque date (the date printed on the cheque).")

    ids = form.getlist("apply_id")
    amounts = form.getlist("apply_amount")
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
