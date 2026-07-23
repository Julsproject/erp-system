"""Delivery Management: fulfillment of an already-completed Sale.

pending (scheduled) -> out_for_delivery (driver has it) -> delivered,
or -> cancelled at any point before delivered. Doesn't touch stock or
costing — that already happened when the underlying Sale was made.

Open to cashiers as well as admins: this is operational (arranging a
delivery right after ringing up a sale), not back-office/financial like
Purchasing or Suppliers.
"""
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from . import audit, models
from .database import get_db
from .deps import get_current_user
from .templating import templates

router = APIRouter()

PAGE_SIZE = 20
STATUS_LABELS = {"pending": "Pending", "out_for_delivery": "Out for Delivery", "delivered": "Delivered", "cancelled": "Cancelled"}

# How a driver can collect a COD balance. Cheque is deliberately excluded: a
# post-dated cheque doesn't settle anything on the spot, and handing the goods
# over against one is a decision for the office, not the driver.
COD_METHODS = [("cash", "Cash"), ("gcash", "GCash"), ("card", "Card"), ("bank_transfer", "Bank Transfer")]

ZERO = Decimal("0")


def _parse_date(s: str):
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def _local_date(col):
    return func.date(func.timezone("Asia/Manila", col))


def _dec(value, default="0") -> Decimal:
    try:
        return Decimal(str(value).strip().replace(",", "") or default)
    except Exception:
        return Decimal(default)


def _outstanding(db: Session, sale) -> Decimal:
    """What's still owed on a sale — its receivable minus everything settled."""
    if not sale:
        return ZERO
    paid = (
        db.query(func.coalesce(func.sum(models.ReceivableSettlement.amount), 0))
        .filter(models.ReceivableSettlement.sale_id == sale.id)
        .scalar()
    )
    return Decimal(str(sale.receivable_amount or 0)) - Decimal(str(paid or 0))


def cod_pending_sale_ids(db: Session) -> set:
    """Sale ids whose balance is waiting on a COD delivery that hasn't been
    delivered or cancelled yet. Used to tell "awaiting delivery" apart from
    ordinary overdue credit in the Receivables list and in notifications."""
    rows = (
        db.query(models.Delivery.sale_id)
        .filter(
            models.Delivery.is_cod.is_(True),
            models.Delivery.status.in_(("pending", "out_for_delivery")),
        )
        .distinct()
        .all()
    )
    return {r[0] for r in rows if r[0]}


@router.get("/deliveries", response_class=HTMLResponse)
def list_deliveries(
    request: Request,
    status_filter: str = "",
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    q = (q or "").strip()
    page = max(page, 1)
    df, dt = _parse_date(date_from), _parse_date(date_to)

    query = db.query(models.Delivery).outerjoin(models.Sale, models.Delivery.sale_id == models.Sale.id)
    if status_filter in STATUS_LABELS:
        query = query.filter(models.Delivery.status == status_filter)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            models.Delivery.delivery_no.ilike(like),
            models.Delivery.recipient_name.ilike(like),
            models.Delivery.driver_name.ilike(like),
            models.Sale.invoice_no.ilike(like),
        ))
    if df:
        query = query.filter(_local_date(models.Delivery.created_at) >= df)
    if dt:
        query = query.filter(_local_date(models.Delivery.created_at) <= dt)

    total = query.count()
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, pages)
    deliveries = (
        query.order_by(models.Delivery.id.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
        .all()
    )
    counts = {s: db.query(models.Delivery).filter(models.Delivery.status == s).count() for s in STATUS_LABELS}

    return templates.TemplateResponse(
        "deliveries/list.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "deliveries": deliveries, "status_filter": status_filter, "counts": counts, "labels": STATUS_LABELS,
            "q": q, "date_from": date_from, "date_to": date_to,
            "page": page, "pages": pages, "total": total,
        },
    )


@router.get("/deliveries/new", response_class=HTMLResponse)
def new_delivery(
    request: Request,
    sale_id: int = 0,
    error: str = "",
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    sale = db.get(models.Sale, sale_id) if sale_id else None
    if sale_id and (not sale or sale.txn_type == "refund"):
        sale = None
        error = error or "That invoice can't be delivered."
    return templates.TemplateResponse(
        "deliveries/new.html",
        {"request": request, "app_name": request.app.title, "user": user, "sale": sale, "error": error,
         "outstanding": _outstanding(db, sale)},
    )


@router.post("/deliveries")
async def create_delivery(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    sale_id = int(form.get("sale_id") or 0)
    sale = db.get(models.Sale, sale_id) if sale_id else None
    if not sale or sale.txn_type == "refund":
        return RedirectResponse("/deliveries/new?error=Pick+a+valid+invoice+first.", status_code=302)

    # COD is only meaningful when the sale still has a balance to collect.
    outstanding = _outstanding(db, sale)
    is_cod = bool(form.get("is_cod")) and outstanding > 0

    delivery = models.Delivery(
        sale_id=sale.id,
        recipient_name=(form.get("recipient_name") or sale.customer_name or "").strip() or None,
        address=(form.get("address") or "").strip() or None,
        contact_no=(form.get("contact_no") or "").strip() or None,
        driver_name=(form.get("driver_name") or "").strip() or None,
        vehicle=(form.get("vehicle") or "").strip() or None,
        scheduled_date=_parse_date((form.get("scheduled_date") or "").strip()),
        notes=(form.get("notes") or "").strip() or None,
        is_cod=is_cod,
        cod_amount=outstanding if is_cod else ZERO,
        created_by=user.id,
    )
    db.add(delivery)
    db.flush()
    delivery.delivery_no = f"DEL-{delivery.id:06d}"
    audit.record(
        db, user=user, request=request, action="create", entity_type="delivery",
        entity_id=delivery.id, entity_label=delivery.delivery_no,
        summary=(f"Scheduled COD delivery {delivery.delivery_no} for invoice {sale.invoice_no}"
                 f" — {outstanding} to collect" if is_cod else
                 f"Scheduled delivery {delivery.delivery_no} for invoice {sale.invoice_no}"),
    )
    db.commit()
    return RedirectResponse(f"/deliveries/{delivery.id}", status_code=status.HTTP_302_FOUND)


@router.get("/deliveries/{delivery_id:int}", response_class=HTMLResponse)
def view_delivery(
    delivery_id: int,
    request: Request,
    error: str = "",
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    delivery = db.get(models.Delivery, delivery_id)
    if not delivery:
        return RedirectResponse("/deliveries", status_code=302)
    return templates.TemplateResponse(
        "deliveries/view.html",
        {"request": request, "app_name": request.app.title, "user": user, "delivery": delivery,
         "labels": STATUS_LABELS, "cod_methods": COD_METHODS, "error": error,
         "outstanding": _outstanding(db, delivery.sale)},
    )


@router.get("/deliveries/{delivery_id:int}/edit", response_class=HTMLResponse)
def edit_delivery(delivery_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    delivery = db.get(models.Delivery, delivery_id)
    if not delivery or delivery.status in ("delivered", "cancelled"):
        return RedirectResponse(f"/deliveries/{delivery_id}", status_code=302)
    return templates.TemplateResponse(
        "deliveries/edit.html",
        {"request": request, "app_name": request.app.title, "user": user, "delivery": delivery},
    )


@router.post("/deliveries/{delivery_id:int}")
async def update_delivery(delivery_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    delivery = db.get(models.Delivery, delivery_id)
    if not delivery or delivery.status in ("delivered", "cancelled"):
        return RedirectResponse(f"/deliveries/{delivery_id}", status_code=302)
    form = await request.form()
    delivery.recipient_name = (form.get("recipient_name") or "").strip() or None
    delivery.address = (form.get("address") or "").strip() or None
    delivery.contact_no = (form.get("contact_no") or "").strip() or None
    delivery.driver_name = (form.get("driver_name") or "").strip() or None
    delivery.vehicle = (form.get("vehicle") or "").strip() or None
    delivery.scheduled_date = _parse_date((form.get("scheduled_date") or "").strip())
    delivery.notes = (form.get("notes") or "").strip() or None
    db.commit()
    return RedirectResponse(f"/deliveries/{delivery_id}", status_code=status.HTTP_302_FOUND)


@router.post("/deliveries/{delivery_id:int}/dispatch")
def dispatch_delivery(delivery_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    delivery = db.get(models.Delivery, delivery_id)
    if delivery and delivery.status == "pending":
        delivery.status = "out_for_delivery"
        delivery.dispatched_at = func.now()
        audit.record(
            db, user=user, request=request, action="dispatch", entity_type="delivery",
            entity_id=delivery.id, entity_label=delivery.delivery_no,
            summary=f"Dispatched {delivery.delivery_no} (out for delivery)",
        )
        db.commit()
    return RedirectResponse(f"/deliveries/{delivery_id}", status_code=status.HTTP_302_FOUND)


@router.post("/deliveries/{delivery_id:int}/complete")
async def complete_delivery(delivery_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Mark a delivery delivered. For a COD delivery this is also the moment the
    driver's collection is recorded — it creates the ReceivableSettlement that
    finally pays down the sale, so handover and payment can't drift apart."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    delivery = db.get(models.Delivery, delivery_id)
    if not delivery or delivery.status not in ("pending", "out_for_delivery"):
        return RedirectResponse(f"/deliveries/{delivery_id}", status_code=status.HTTP_302_FOUND)

    form = await request.form()
    outstanding = _outstanding(db, delivery.sale)
    collected = ZERO

    if delivery.is_cod and outstanding > 0:
        method = (form.get("collect_method") or "cash").strip().lower()
        if method not in dict(COD_METHODS):
            method = "cash"
        collected = _dec(form.get("collect_amount"))
        if collected <= 0:
            return RedirectResponse(
                f"/deliveries/{delivery_id}?error=Enter+the+amount+the+driver+collected.",
                status_code=status.HTTP_302_FOUND,
            )
        if collected > outstanding:
            collected = outstanding  # never collect more than is owed

        settlement = models.ReceivableSettlement(
            sale_id=delivery.sale_id, method=method, amount=collected, cashier_id=user.id,
        )
        db.add(settlement)
        db.flush()
        delivery.settlement_id = settlement.id
        delivery.collected_amount = collected
        delivery.collected_method = method
        delivery.collected_at = func.now()

    delivery.status = "delivered"
    delivery.delivered_at = func.now()
    audit.record(
        db, user=user, request=request, action="complete", entity_type="delivery",
        entity_id=delivery.id, entity_label=delivery.delivery_no,
        summary=(f"Delivered {delivery.delivery_no} and collected {collected} "
                 f"({delivery.collected_method}) on COD" if collected > 0
                 else f"Marked {delivery.delivery_no} delivered"),
    )
    db.commit()
    return RedirectResponse(f"/deliveries/{delivery_id}", status_code=status.HTTP_302_FOUND)


@router.post("/deliveries/{delivery_id:int}/cancel")
def cancel_delivery(delivery_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    delivery = db.get(models.Delivery, delivery_id)
    if delivery and delivery.status in ("pending", "out_for_delivery"):
        delivery.status = "cancelled"
        delivery.cancelled_at = func.now()
        audit.record(
            db, user=user, request=request, action="cancel", entity_type="delivery",
            entity_id=delivery.id, entity_label=delivery.delivery_no,
            summary=f"Cancelled {delivery.delivery_no}",
        )
        db.commit()
    return RedirectResponse(f"/deliveries/{delivery_id}", status_code=status.HTTP_302_FOUND)
