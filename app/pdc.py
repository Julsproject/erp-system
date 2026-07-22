"""Post-dated cheque (PDC) register.

A PDC holds a payment in limbo until the bank actually honors it:
  received (from a customer settling credit) -> clearing creates the
    ReceivableSettlement only now, so the credit balance doesn't drop until
    the cheque is proven good.
  issued (to pay a supplier) -> clearing marks that Purchase paid only now.
Bouncing a received cheque needs no reversal (nothing was ever applied);
bouncing an issued one just leaves the purchase unpaid.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models
from .database import get_db
from .deps import get_current_user, is_admin
from .templating import templates

router = APIRouter()

MANILA = ZoneInfo("Asia/Manila")
STATUS_LABELS = {"pending": "Pending", "cleared": "Cleared", "bounced": "Bounced", "cancelled": "Cancelled"}
DUE_SOON_DAYS = 3


def _today() -> date:
    return datetime.now(MANILA).date()


@router.get("/pdc", response_class=HTMLResponse)
def list_pdc(
    request: Request,
    direction: str = "",
    status_filter: str = "",
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)

    query = db.query(models.PostDatedCheque)
    if direction in ("received", "issued"):
        query = query.filter(models.PostDatedCheque.direction == direction)
    if status_filter in STATUS_LABELS:
        query = query.filter(models.PostDatedCheque.status == status_filter)

    # Pending ones soonest-due first; resolved ones most-recent first.
    if status_filter == "pending" or not status_filter:
        rows = query.order_by(
            (models.PostDatedCheque.status != "pending"),
            models.PostDatedCheque.cheque_date,
        ).all()
    else:
        rows = query.order_by(models.PostDatedCheque.cheque_date.desc()).all()

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
            "today": today, "horizon": horizon,
        },
    )


@router.get("/pdc/{pdc_id:int}", response_class=HTMLResponse)
def view_pdc(pdc_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    pdc = db.get(models.PostDatedCheque, pdc_id)
    if not pdc:
        return RedirectResponse("/pdc", status_code=302)
    return templates.TemplateResponse(
        "pdc/view.html",
        {"request": request, "app_name": request.app.title, "user": user, "pdc": pdc, "today": _today()},
    )


@router.post("/pdc/{pdc_id:int}/clear")
def clear_pdc(pdc_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """The bank honored it: apply the payment it represents, only now."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    pdc = db.get(models.PostDatedCheque, pdc_id)
    if not pdc or pdc.status != "pending":
        return RedirectResponse(f"/pdc/{pdc_id}", status_code=302)

    if pdc.direction == "received":
        settlement = models.ReceivableSettlement(
            sale_id=pdc.sale_id, method="cheque", amount=pdc.amount,
            bank=pdc.bank, cheque_no=pdc.cheque_no, cheque_date=pdc.cheque_date.isoformat(),
            cashier_id=user.id,
        )
        db.add(settlement)
        db.flush()
        pdc.settlement_id = settlement.id
    elif pdc.purchase_id:
        purchase = db.get(models.Purchase, pdc.purchase_id)
        if purchase:
            purchase.status = "paid"
            purchase.paid_at = func.now()

    pdc.status = "cleared"
    pdc.resolved_at = func.now()
    db.commit()
    return RedirectResponse(f"/pdc/{pdc_id}", status_code=status.HTTP_302_FOUND)


@router.post("/pdc/{pdc_id:int}/bounce")
async def bounce_pdc(pdc_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """The bank rejected it. Received: nothing to undo. Issued: stays unpaid."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    pdc = db.get(models.PostDatedCheque, pdc_id)
    if not pdc or pdc.status != "pending":
        return RedirectResponse(f"/pdc/{pdc_id}", status_code=302)

    form = await request.form()
    note = (form.get("notes") or "").strip()
    if note:
        pdc.notes = note
    pdc.status = "bounced"
    pdc.resolved_at = func.now()
    db.commit()
    return RedirectResponse(f"/pdc/{pdc_id}", status_code=status.HTTP_302_FOUND)


@router.post("/pdc/{pdc_id:int}/cancel")
def cancel_pdc(pdc_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """The cheque was returned/replaced before ever being deposited."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    pdc = db.get(models.PostDatedCheque, pdc_id)
    if pdc and pdc.status == "pending":
        pdc.status = "cancelled"
        pdc.resolved_at = func.now()
        db.commit()
    return RedirectResponse(f"/pdc/{pdc_id}", status_code=status.HTTP_302_FOUND)
