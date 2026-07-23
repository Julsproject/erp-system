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


def _parse_date(s: str):
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def _local_date(col):
    return func.date(func.timezone("Asia/Manila", col))


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
        {"request": request, "app_name": request.app.title, "user": user, "sale": sale, "error": error},
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

    delivery = models.Delivery(
        sale_id=sale.id,
        recipient_name=(form.get("recipient_name") or sale.customer_name or "").strip() or None,
        address=(form.get("address") or "").strip() or None,
        contact_no=(form.get("contact_no") or "").strip() or None,
        driver_name=(form.get("driver_name") or "").strip() or None,
        vehicle=(form.get("vehicle") or "").strip() or None,
        scheduled_date=_parse_date((form.get("scheduled_date") or "").strip()),
        notes=(form.get("notes") or "").strip() or None,
        created_by=user.id,
    )
    db.add(delivery)
    db.flush()
    delivery.delivery_no = f"DEL-{delivery.id:06d}"
    audit.record(
        db, user=user, request=request, action="create", entity_type="delivery",
        entity_id=delivery.id, entity_label=delivery.delivery_no,
        summary=f"Scheduled delivery {delivery.delivery_no} for invoice {sale.invoice_no}",
    )
    db.commit()
    return RedirectResponse(f"/deliveries/{delivery.id}", status_code=status.HTTP_302_FOUND)


@router.get("/deliveries/{delivery_id:int}", response_class=HTMLResponse)
def view_delivery(delivery_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    delivery = db.get(models.Delivery, delivery_id)
    if not delivery:
        return RedirectResponse("/deliveries", status_code=302)
    return templates.TemplateResponse(
        "deliveries/view.html",
        {"request": request, "app_name": request.app.title, "user": user, "delivery": delivery, "labels": STATUS_LABELS},
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
def complete_delivery(delivery_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    delivery = db.get(models.Delivery, delivery_id)
    if delivery and delivery.status in ("pending", "out_for_delivery"):
        delivery.status = "delivered"
        delivery.delivered_at = func.now()
        audit.record(
            db, user=user, request=request, action="complete", entity_type="delivery",
            entity_id=delivery.id, entity_label=delivery.delivery_no,
            summary=f"Marked {delivery.delivery_no} delivered",
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
