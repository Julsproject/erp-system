"""Point of Sale.

POS v1: search products, sell by any unit from the ladder, per-line and overall
discount, VAT (12% inclusive) computation, single payment + change, inventory
deduction in base units, printable receipt.

Deferred: customers/receivable, split payments, open-container display, returns.
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from . import models
from .customers import get_or_create_customer
from .database import get_db
from .deps import get_current_user
from .templating import templates

router = APIRouter()

METHOD_LABELS = {
    "cash": "Cash",
    "gcash": "GCash",
    "card": "Card",
    "bank_transfer": "Bank Transfer",
    "receivable": "Receivable",
}

VAT_RATE = Decimal("0.12")
VAT_DIVISOR = Decimal("1.12")
CENTS = Decimal("0.01")


def _dec(value, default="0") -> Decimal:
    try:
        return Decimal(str(value).strip().replace(",", "") or default)
    except (InvalidOperation, AttributeError, ValueError):
        return Decimal(default)


def _money(value) -> Decimal:
    return _dec(value).quantize(CENTS, rounding=ROUND_HALF_UP)


def _deduct_stock(product: models.Product, base_qty: Decimal):
    """Reduce on-hand by base_qty, taking from received stock first, then beginning."""
    take = min(base_qty, product.stock_qty or Decimal("0"))
    product.stock_qty = (product.stock_qty or Decimal("0")) - take
    remainder = base_qty - take
    if remainder > 0:
        product.beginning_stock = (product.beginning_stock or Decimal("0")) - remainder


def _add_stock(product: models.Product, base_qty: Decimal):
    """Add base_qty back to on-hand (used by refunds and exchange returns)."""
    product.stock_qty = (product.stock_qty or Decimal("0")) + base_qty


@router.get("/pos", response_class=HTMLResponse)
def pos_page(request: Request, user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(
        "pos.html",
        {"request": request, "app_name": request.app.title, "user": user},
    )


@router.get("/pos/search")
def pos_search(q: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    q = (q or "").strip()
    query = db.query(models.Product).filter(models.Product.is_active.is_(True))
    if q:
        query = query.filter(models.Product.name.ilike(f"%{q}%"))
    products = query.order_by(models.Product.name).limit(30).all()

    out = []
    for p in products:
        base_unit = p.unit_type.name if p.unit_type else "Unit"
        units = [{"name": base_unit, "factor": 1.0, "price": float(p.selling_price or 0)}]
        for u in p.units:
            units.append({"name": u.name, "factor": float(u.factor_to_base or 1), "price": float(u.price or 0)})
        out.append({
            "id": p.id,
            "name": p.name,
            "is_vat": bool(p.is_vat),
            "base_unit": base_unit,
            "on_hand": float((p.beginning_stock or 0) + (p.stock_qty or 0)),
            "units": units,
        })
    return {"products": out}


@router.post("/pos/checkout")
async def pos_checkout(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    data = await request.json()
    lines = data.get("lines") or []
    if not lines:
        return JSONResponse({"ok": False, "error": "Cart is empty."}, status_code=400)

    invoice_no = (data.get("invoice_no") or "").strip()
    if not invoice_no:
        return JSONResponse({"ok": False, "error": "Invoice number is required."}, status_code=400)
    if db.query(models.Sale).filter(models.Sale.invoice_no == invoice_no).first():
        return JSONResponse(
            {"ok": False, "error": f"Invoice number '{invoice_no}' is already used."}, status_code=400
        )

    customer_name = (data.get("customer_name") or "").strip()
    vat_applied = bool(data.get("vat_applied"))
    sale = models.Sale(invoice_no=invoice_no, customer_name=customer_name or None, cashier_id=user.id)
    db.add(sale)

    subtotal = Decimal("0")

    for ln in lines:
        product = db.get(models.Product, int(ln["product_id"]))
        if not product:
            continue
        qty = _dec(ln.get("qty"))
        unit_price = _dec(ln.get("unit_price"))
        factor = _dec(ln.get("factor"), "1")
        discount = _dec(ln.get("discount"))
        is_vat = vat_applied  # VAT is now a whole-transaction toggle

        line_total = qty * unit_price - discount
        if line_total < 0:
            line_total = Decimal("0")
        subtotal += line_total

        base_qty = qty * factor
        _deduct_stock(product, base_qty)
        db.add(models.StockMovement(product_id=product.id, qty_base=-base_qty, reason="sale"))

        sale.lines.append(models.SaleLine(
            product_id=product.id,
            product_name=product.name,
            unit_name=ln.get("unit_name"),
            unit_factor=factor,
            qty=qty,
            unit_price=unit_price,
            discount=discount,
            line_total=_money(line_total),
            is_vat=is_vat,
        ))

    discount_total = _dec(data.get("discount_total"))
    total = subtotal - discount_total
    if total < 0:
        total = Decimal("0")
    # VAT is 12% inclusive on the whole transaction when the cashier toggles it on.
    vat_amount = _money(total / VAT_DIVISOR * VAT_RATE) if vat_applied else Decimal("0")

    # --- Payments (split) ---------------------------------------------------
    payments_in = data.get("payments") or []
    receivable_amount = Decimal("0")
    paid_amount = Decimal("0")
    method_rows = []
    for pay in payments_in:
        method = (pay.get("method") or "").strip().lower()
        amount = _dec(pay.get("amount"))
        if amount <= 0 or method not in METHOD_LABELS:
            continue
        method_rows.append((method, amount))
        if method == "receivable":
            receivable_amount += amount
        else:
            paid_amount += amount

    if not method_rows:
        return JSONResponse({"ok": False, "error": "Add at least one payment."}, status_code=400)

    if receivable_amount > total:
        receivable_amount = total
    if receivable_amount > 0 and not customer_name:
        return JSONResponse(
            {"ok": False, "error": "Receivable (utang) requires a customer name."}, status_code=400
        )

    amount_due_now = total - receivable_amount
    if paid_amount + Decimal("0.01") < amount_due_now:
        short = amount_due_now - paid_amount
        return JSONResponse(
            {"ok": False, "error": f"Payment is short by ₱{short:.2f}. Add a payment or receivable."},
            status_code=400,
        )
    change = paid_amount - amount_due_now
    if change < 0:
        change = Decimal("0")

    # Attach customer (create by name if needed) when there is utang or a name.
    customer = get_or_create_customer(db, customer_name) if customer_name else None
    if customer:
        sale.customer_id = customer.id

    for method, amount in method_rows:
        sale.payments.append(models.Payment(method=method, amount=_money(amount)))

    sale.subtotal = _money(subtotal)
    sale.discount_total = _money(discount_total)
    sale.vat_amount = vat_amount
    sale.net_amount = _money(total - vat_amount)
    sale.total = _money(total)
    sale.amount_tendered = _money(paid_amount)
    sale.change_amount = _money(change)
    sale.receivable_amount = _money(receivable_amount)
    sale.payment_method = " + ".join(
        dict.fromkeys(METHOD_LABELS[m] for m, _ in method_rows)  # unique, order-preserving
    )

    db.commit()

    return {"ok": True, "sale_id": sale.id, "invoice_no": sale.invoice_no}


@router.get("/pos/lookup")
def pos_lookup(invoice: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Find an original SALE by invoice number, for refund/exchange."""
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    invoice = (invoice or "").strip()
    sale = (
        db.query(models.Sale)
        .filter(models.Sale.invoice_no == invoice, models.Sale.txn_type == "sale")
        .first()
    )
    if not sale:
        return {"found": False}
    lines = [{
        "product_id": l.product_id,
        "name": l.product_name,
        "unit_name": l.unit_name,
        "factor": float(l.unit_factor or 1),
        "qty": float(l.qty or 0),
        "unit_price": float(l.unit_price or 0),
        "is_vat": bool(l.is_vat),
    } for l in sale.lines]
    return {
        "found": True,
        "sale_id": sale.id,
        "invoice_no": sale.invoice_no,
        "customer_name": sale.customer_name or "",
        "date": sale.created_at.strftime("%b %d, %Y %I:%M %p") if sale.created_at else "",
        "lines": lines,
    }


@router.post("/pos/refund")
async def pos_refund(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    data = await request.json()
    orig = db.get(models.Sale, int(data.get("sale_id") or 0)) if data.get("sale_id") else None
    items = data.get("items") or []
    if not items:
        return JSONResponse({"ok": False, "error": "Select at least one item to refund."}, status_code=400)

    refund = models.Sale(
        txn_type="refund",
        original_sale_id=orig.id if orig else None,
        customer_name=(orig.customer_name if orig else None),
        customer_id=(orig.customer_id if orig else None),
        cashier_id=user.id,
    )
    db.add(refund)

    total = Decimal("0")
    vat = Decimal("0")
    for it in items:
        qty = _dec(it.get("qty"))
        if qty <= 0:
            continue
        unit_price = _dec(it.get("unit_price"))
        factor = _dec(it.get("factor"), "1")
        is_vat = bool(it.get("is_vat"))
        value = qty * unit_price
        total += value
        if is_vat:
            vat += value / VAT_DIVISOR * VAT_RATE
        product = db.get(models.Product, int(it["product_id"])) if it.get("product_id") else None
        if product:
            _add_stock(product, qty * factor)
            db.add(models.StockMovement(product_id=product.id, qty_base=qty * factor, reason="refund"))
        refund.lines.append(models.SaleLine(
            product_id=product.id if product else None,
            product_name=it.get("name") or "Item",
            unit_name=it.get("unit_name"),
            unit_factor=factor,
            qty=qty,
            unit_price=unit_price,
            discount=Decimal("0"),
            line_total=_money(-value),
            is_vat=is_vat,
        ))

    if total <= 0:
        return JSONResponse({"ok": False, "error": "Nothing to refund."}, status_code=400)

    refund.subtotal = _money(-total)
    refund.total = _money(-total)
    refund.vat_amount = _money(-vat)
    refund.net_amount = _money(-(total - vat))
    refund.payment_method = "Cash refund"
    refund.amount_tendered = Decimal("0")
    refund.change_amount = _money(total)  # cash paid out to customer
    db.flush()
    refund.invoice_no = f"REF-{refund.id:06d}"
    db.commit()
    return {"ok": True, "sale_id": refund.id, "invoice_no": refund.invoice_no}


@router.post("/pos/exchange")
async def pos_exchange(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    data = await request.json()
    orig = db.get(models.Sale, int(data.get("sale_id") or 0)) if data.get("sale_id") else None
    returned = data.get("returned_items") or []
    new_lines = data.get("new_lines") or []
    if not returned and not new_lines:
        return JSONResponse({"ok": False, "error": "Nothing to exchange."}, status_code=400)

    ex = models.Sale(
        txn_type="exchange",
        original_sale_id=orig.id if orig else None,
        customer_name=(orig.customer_name if orig else (data.get("customer_name") or None)),
        customer_id=(orig.customer_id if orig else None),
        cashier_id=user.id,
    )
    db.add(ex)

    returned_total = Decimal("0")
    new_total = Decimal("0")
    vat = Decimal("0")

    for it in returned:
        qty = _dec(it.get("qty"))
        if qty <= 0:
            continue
        unit_price = _dec(it.get("unit_price"))
        factor = _dec(it.get("factor"), "1")
        value = qty * unit_price
        returned_total += value
        product = db.get(models.Product, int(it["product_id"])) if it.get("product_id") else None
        if product:
            _add_stock(product, qty * factor)
            db.add(models.StockMovement(product_id=product.id, qty_base=qty * factor, reason="exchange-return"))
        ex.lines.append(models.SaleLine(
            product_id=product.id if product else None, product_name=it.get("name") or "Item",
            unit_name=it.get("unit_name"), unit_factor=factor, qty=-qty, unit_price=unit_price,
            discount=Decimal("0"), line_total=_money(-value), is_vat=bool(it.get("is_vat")),
        ))

    for ln in new_lines:
        qty = _dec(ln.get("qty"))
        if qty <= 0:
            continue
        unit_price = _dec(ln.get("unit_price"))
        factor = _dec(ln.get("factor"), "1")
        discount = _dec(ln.get("discount"))
        is_vat = bool(ln.get("is_vat"))
        lt = qty * unit_price - discount
        if lt < 0:
            lt = Decimal("0")
        new_total += lt
        if is_vat:
            vat += lt / VAT_DIVISOR * VAT_RATE
        product = db.get(models.Product, int(ln["product_id"])) if ln.get("product_id") else None
        if product:
            _deduct_stock(product, qty * factor)
            db.add(models.StockMovement(product_id=product.id, qty_base=-(qty * factor), reason="exchange-sale"))
        ex.lines.append(models.SaleLine(
            product_id=product.id if product else None, product_name=ln.get("name") or "Item",
            unit_name=ln.get("unit_name"), unit_factor=factor, qty=qty, unit_price=unit_price,
            discount=discount, line_total=_money(lt), is_vat=is_vat,
        ))

    diff = new_total - returned_total  # >0 customer pays, <0 cash refund to customer
    ex.subtotal = _money(new_total)
    ex.vat_amount = _money(vat)
    ex.total = _money(diff)
    ex.net_amount = _money(diff - vat)
    if diff > 0:
        method = (data.get("payment_method") or "cash").strip().lower()
        if method not in METHOD_LABELS or method == "receivable":
            method = "cash"
        ex.payments.append(models.Payment(method=method, amount=_money(diff)))
        ex.amount_tendered = _money(diff)
        ex.change_amount = Decimal("0")
        ex.payment_method = METHOD_LABELS[method]
    elif diff < 0:
        ex.amount_tendered = Decimal("0")
        ex.change_amount = _money(-diff)  # cash refunded to customer
        ex.payment_method = "Cash refund"
    else:
        ex.payment_method = "Even exchange"

    db.flush()
    ex.invoice_no = f"EXC-{ex.id:06d}"
    db.commit()
    return {"ok": True, "sale_id": ex.id, "invoice_no": ex.invoice_no}


@router.get("/pos/receipt/{sale_id:int}", response_class=HTMLResponse)
def pos_receipt(sale_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    sale = db.get(models.Sale, sale_id)
    if not sale:
        return RedirectResponse("/pos", status_code=302)
    return templates.TemplateResponse(
        "receipt.html",
        {"request": request, "app_name": request.app.title, "user": user, "sale": sale},
    )
