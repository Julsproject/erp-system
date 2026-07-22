"""Quotations: a price estimate for a customer, with a status lifecycle.

pending -> confirmed -> paid (converts into a real Sale via _finalize_sale),
or pending/confirmed -> cancelled. A quotation never touches stock or costing
until it is converted.
"""
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from . import models
from .customers import get_or_create_customer
from .database import get_db
from .deps import get_current_user, is_admin
from .pos import METHOD_LABELS, _finalize_sale, _money, _vat_of
from .templating import templates

router = APIRouter()

STATUS_LABELS = {"pending": "Pending", "confirmed": "Confirmed", "paid": "Paid", "cancelled": "Cancelled"}
PAGE_SIZE = 20


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


def _local_date(col):
    return func.date(func.timezone("Asia/Manila", col))


@router.post("/quotations")
async def create_quotation(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    data = await request.json()
    lines = data.get("lines") or []
    if not lines:
        return JSONResponse({"ok": False, "error": "Add at least one item."}, status_code=400)

    customer_name = (data.get("customer_name") or "").strip()
    vat_applied = bool(data.get("vat_applied"))
    quote = models.Quotation(customer_name=customer_name or None, vat_applied=vat_applied, created_by=user.id)
    db.add(quote)

    subtotal = Decimal("0")
    for ln in lines:
        product = db.get(models.Product, int(ln["product_id"])) if ln.get("product_id") else None
        qty = _dec(ln.get("qty"))
        if qty <= 0:
            continue
        unit_price = _dec(ln.get("unit_price"))
        factor = _dec(ln.get("factor"), "1")
        discount = _dec(ln.get("discount"))
        line_total = qty * unit_price - discount
        if line_total < 0:
            line_total = Decimal("0")
        subtotal += line_total
        quote.lines.append(models.QuotationLine(
            product_id=product.id if product else None,
            product_name=(product.name if product else ln.get("name")) or "Item",
            unit_name=ln.get("unit_name"),
            unit_factor=factor,
            qty=qty,
            unit_price=unit_price,
            discount=discount,
            line_total=_money(line_total),
        ))

    if not quote.lines:
        return JSONResponse({"ok": False, "error": "No valid items."}, status_code=400)

    discount_total = _dec(data.get("discount_total"))
    total = subtotal - discount_total
    if total < 0:
        total = Decimal("0")
    vat_amount = _vat_of(total) if vat_applied else Decimal("0")

    quote.subtotal = _money(subtotal)
    quote.discount_total = _money(discount_total)
    quote.vat_amount = vat_amount
    quote.total = _money(total)

    if customer_name:
        customer = get_or_create_customer(db, customer_name)
        if customer:
            quote.customer_id = customer.id

    db.flush()
    quote.quote_no = f"QUO-{quote.id:06d}"
    db.commit()
    return {"ok": True, "quotation_id": quote.id, "quote_no": quote.quote_no}


@router.get("/quotations", response_class=HTMLResponse)
def list_quotations(
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
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    page = max(page, 1)
    q = (q or "").strip()
    df, dt = _parse_date(date_from), _parse_date(date_to)

    query = db.query(models.Quotation)
    if status_filter in STATUS_LABELS:
        query = query.filter(models.Quotation.status == status_filter)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(models.Quotation.quote_no.ilike(like), models.Quotation.customer_name.ilike(like)))
    if df:
        query = query.filter(_local_date(models.Quotation.created_at) >= df)
    if dt:
        query = query.filter(_local_date(models.Quotation.created_at) <= dt)

    total = query.count()
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, pages)
    quotes = (
        query.order_by(models.Quotation.id.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
        .all()
    )
    counts = {s: db.query(models.Quotation).filter(models.Quotation.status == s).count() for s in STATUS_LABELS}
    return templates.TemplateResponse(
        "quotations/list.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "quotes": quotes, "status_filter": status_filter, "counts": counts, "labels": STATUS_LABELS,
            "q": q, "date_from": date_from, "date_to": date_to,
            "page": page, "pages": pages,
        },
    )


@router.get("/quotations/{quote_id:int}", response_class=HTMLResponse)
def view_quotation(
    quote_id: int,
    request: Request,
    error: str = "",
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    quote = db.get(models.Quotation, quote_id)
    if not quote:
        return RedirectResponse("/quotations", status_code=302)
    return templates.TemplateResponse(
        "quotations/view.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "quote": quote, "error": error, "methods": METHOD_LABELS,
        },
    )


@router.post("/quotations/{quote_id:int}/confirm")
def confirm_quotation(quote_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    quote = db.get(models.Quotation, quote_id)
    if quote and quote.status == "pending":
        quote.status = "confirmed"
        quote.confirmed_at = func.now()
        db.commit()
    return RedirectResponse(f"/quotations/{quote_id}", status_code=status.HTTP_302_FOUND)


@router.post("/quotations/{quote_id:int}/cancel")
def cancel_quotation(quote_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    quote = db.get(models.Quotation, quote_id)
    if quote and quote.status in ("pending", "confirmed"):
        quote.status = "cancelled"
        quote.cancelled_at = func.now()
        db.commit()
    return RedirectResponse(f"/quotations/{quote_id}", status_code=status.HTTP_302_FOUND)


@router.post("/quotations/{quote_id:int}/reopen")
def reopen_quotation(quote_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Bring a cancelled quotation back to pending, e.g. the customer changed their mind."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    quote = db.get(models.Quotation, quote_id)
    if quote and quote.status == "cancelled":
        quote.status = "pending"
        quote.cancelled_at = None
        db.commit()
    return RedirectResponse(f"/quotations/{quote_id}", status_code=status.HTTP_302_FOUND)


@router.post("/quotations/{quote_id:int}/convert")
async def convert_quotation(
    quote_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Turn a pending/confirmed quotation into a real, paid Sale.

    Accepts JSON like POS checkout does, with a `payments` array — so a
    quotation can be settled with split payment (part cash, part GCash, part
    receivable) exactly like a normal sale.
    """
    if not user:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    quote = db.get(models.Quotation, quote_id)
    if not quote or quote.status not in ("pending", "confirmed"):
        return JSONResponse({"ok": False, "error": "This quotation can no longer be converted."}, status_code=400)

    data = await request.json()
    invoice_no = (data.get("invoice_no") or "").strip()
    payments = data.get("payments") or []

    lines = [{
        "product_id": l.product_id, "unit_name": l.unit_name, "factor": l.unit_factor,
        "qty": l.qty, "unit_price": l.unit_price, "discount": l.discount,
    } for l in quote.lines if l.product_id]

    if not lines:
        return JSONResponse(
            {"ok": False, "error": "None of the quoted items exist in inventory anymore."}, status_code=400
        )

    ok, result = _finalize_sale(
        db, user,
        invoice_no=invoice_no,
        customer_name=quote.customer_name,
        vat_applied=quote.vat_applied,
        discount_total=quote.discount_total,
        lines=lines,
        payments=payments,
    )
    if not ok:
        return JSONResponse({"ok": False, "error": result}, status_code=400)

    sale = result
    quote.status = "paid"
    quote.converted_sale_id = sale.id
    quote.paid_at = func.now()
    db.commit()
    return {"ok": True, "sale_id": sale.id, "invoice_no": sale.invoice_no}
