"""Point of Sale.

POS v1: search products, sell by any unit from the ladder, per-line and overall
discount, VAT (12% inclusive) computation, single payment + change, inventory
deduction in base units, printable receipt.

Deferred: customers/receivable, split payments, open-container display, returns.
"""
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func
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
    "cheque": "Cheque",
    "receivable": "Receivable",
}

# VAT is INCLUSIVE: the selling price already contains it.
#   net of VAT = total / 1.12        VAT = total - net of VAT
VAT_RATE = Decimal("0.12")
VAT_DIVISOR = Decimal("1.12")
CENTS = Decimal("0.01")


def _vat_of(gross: Decimal) -> Decimal:
    """The VAT portion contained in a VAT-inclusive amount."""
    return _money(gross * VAT_RATE / VAT_DIVISOR)


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


def _linked_ref(db: Session, prefix: str, orig) -> str | None:
    """Build a reference that points at the original sale, e.g. REF-45.

    Partial refunds of the same invoice would collide, so a counter is added
    (REF-45-2, REF-45-3...). The result is kept within the 20-char column.
    """
    if not orig or not orig.invoice_no:
        return None
    base = f"{prefix}-{orig.invoice_no}"[:20]
    candidate, n = base, 1
    while db.query(models.Sale).filter(models.Sale.invoice_no == candidate).first():
        n += 1
        suffix = f"-{n}"
        candidate = f"{base[:20 - len(suffix)]}{suffix}"
    return candidate


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
        c = p.container
        container = None if not c else {
            "pack_name": c["pack_name"],
            "loose_name": c["loose_name"],
            "sealed": c["sealed"],
            "open": float(c["open"]),
        }
        out.append({
            "id": p.id,
            "name": p.name,
            "is_vat": bool(p.is_vat),
            "base_unit": base_unit,
            "on_hand": float((p.beginning_stock or 0) + (p.stock_qty or 0)),
            "units": units,
            "container": container,
        })
    return {"products": out}


def _finalize_sale(db: Session, user, *, invoice_no, customer_name, vat_applied, discount_total, lines, payments):
    """Create and commit a real Sale from line items + payments.

    Shared by POS checkout and by quotations converting to a paid sale, so the
    stock/cost/VAT/receivable math only lives in one place.
    Returns (True, sale) on success, or (False, error_message) on failure.
    """
    invoice_no = (invoice_no or "").strip()
    if not invoice_no:
        return False, "Invoice number is required."
    if db.query(models.Sale).filter(models.Sale.invoice_no == invoice_no).first():
        return False, f"Invoice number '{invoice_no}' is already used."

    customer_name = (customer_name or "").strip()
    vat_applied = bool(vat_applied)
    sale = models.Sale(invoice_no=invoice_no, customer_name=customer_name or None, cashier_id=user.id)
    db.add(sale)

    subtotal = Decimal("0")

    for ln in lines:
        product = db.get(models.Product, int(ln["product_id"])) if ln.get("product_id") else None
        if not product:
            continue
        qty = _dec(ln.get("qty"))
        unit_price = _dec(ln.get("unit_price"))
        factor = _dec(ln.get("factor"), "1")
        discount = _dec(ln.get("discount"))
        is_vat = vat_applied  # VAT is a whole-transaction toggle

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
            # Freeze today's cost so profit reporting stays accurate later.
            unit_cost=_money(product.cost_price or 0),
        ))

    discount_total = _dec(discount_total)
    total = subtotal - discount_total
    if total < 0:
        total = Decimal("0")
    # VAT is already inside the price — extract it, don't add it. The customer
    # pays the same whether VAT is ticked or not; it only splits the receipt.
    vat_amount = _vat_of(total) if vat_applied else Decimal("0")
    net = _money(total) - vat_amount

    # --- Payments (split) ---------------------------------------------------
    # A cheque is post-dated: like Receivable, it isn't cash in hand yet, so it
    # counts toward receivable_amount rather than paid_amount. It stays owed
    # until the cheque actually clears (see /pdc), which is when a
    # ReceivableSettlement finally gets created — the same mechanism a credit
    # sale uses when a customer later pays off their balance by cheque.
    receivable_amount = Decimal("0")
    paid_amount = Decimal("0")
    method_rows = []
    cheque_rows = []
    for pay in payments or []:
        method = (pay.get("method") or "").strip().lower()
        amount = _dec(pay.get("amount"))
        if amount <= 0 or method not in METHOD_LABELS:
            continue
        method_rows.append((method, amount))
        if method in ("receivable", "cheque"):
            receivable_amount += amount
        else:
            paid_amount += amount
        if method == "cheque":
            raw_date = (pay.get("cheque_date") or "").strip()
            try:
                cheque_date = date.fromisoformat(raw_date)
            except ValueError:
                return False, "Enter a valid cheque date (the date printed on the cheque)."
            cheque_rows.append({
                "amount": amount,
                "bank": (pay.get("bank") or "").strip() or None,
                "cheque_no": (pay.get("cheque_no") or "").strip() or None,
                "cheque_date": cheque_date,
            })

    if not method_rows:
        return False, "Add at least one payment."

    if receivable_amount > total:
        receivable_amount = total
    if receivable_amount > 0 and not customer_name:
        return False, "Receivable (credit) or cheque payment requires a customer name."

    amount_due_now = total - receivable_amount
    if paid_amount + Decimal("0.01") < amount_due_now:
        short = amount_due_now - paid_amount
        return False, f"Payment is short by ₱{short:.2f}. Add a payment, cheque, or receivable."
    change = paid_amount - amount_due_now
    if change < 0:
        change = Decimal("0")

    # Attach customer (create by name if needed) when there is credit or a name.
    customer = get_or_create_customer(db, customer_name) if customer_name else None
    if customer:
        sale.customer_id = customer.id
        # Credit (and post-dated cheques) fall due after the customer's agreed terms.
        if receivable_amount > 0:
            days = customer.credit_days if customer.credit_days is not None else 15
            sale.due_date = date.today() + timedelta(days=int(days))

    for method, amount in method_rows:
        sale.payments.append(models.Payment(method=method, amount=_money(amount)))

    sale.subtotal = _money(subtotal)
    sale.discount_total = _money(discount_total)
    sale.vat_amount = vat_amount
    sale.net_amount = _money(net)
    sale.total = _money(total)
    sale.amount_tendered = _money(paid_amount)
    sale.change_amount = _money(change)
    sale.receivable_amount = _money(receivable_amount)
    sale.payment_method = " + ".join(
        dict.fromkeys(METHOD_LABELS[m] for m, _ in method_rows)  # unique, order-preserving
    )

    db.flush()  # need sale.id / sale.customer_id before creating the cheque records below
    for row in cheque_rows:
        db.add(models.PostDatedCheque(
            direction="received", amount=_money(row["amount"]),
            bank=row["bank"], cheque_no=row["cheque_no"], cheque_date=row["cheque_date"],
            sale_id=sale.id, customer_id=sale.customer_id,
            created_by=user.id,
        ))

    db.commit()
    return True, sale


@router.post("/pos/checkout")
async def pos_checkout(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    data = await request.json()
    lines = data.get("lines") or []
    if not lines:
        return JSONResponse({"ok": False, "error": "Cart is empty."}, status_code=400)

    ok, result = _finalize_sale(
        db, user,
        invoice_no=data.get("invoice_no"),
        customer_name=data.get("customer_name"),
        vat_applied=data.get("vat_applied"),
        discount_total=data.get("discount_total"),
        lines=lines,
        payments=data.get("payments") or [],
    )
    if not ok:
        return JSONResponse({"ok": False, "error": result}, status_code=400)

    sale = result
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

    total = Decimal("0")       # net (VAT-exclusive) value of the refunded items
    vat_base = Decimal("0")    # the part of that which was sold with VAT on top
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
            vat_base += value
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

    # Prices already include VAT, so the customer gets back exactly what they
    # paid; the VAT portion is extracted out of that amount for reporting.
    gross = _money(total)
    vat = _vat_of(vat_base)

    refund.subtotal = -gross
    refund.net_amount = -(gross - vat)
    refund.vat_amount = -vat
    refund.total = -gross
    refund.payment_method = "Cash refund"
    refund.amount_tendered = Decimal("0")
    refund.change_amount = gross  # cash paid out to customer
    db.flush()
    # Point the refund at the invoice it came from (REF-45); fall back to a
    # sequential number for refunds with no original invoice.
    refund.invoice_no = _linked_ref(db, "REF", orig) or f"REF-{refund.id:06d}"
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

    vat_applied = bool(data.get("vat_applied"))   # VAT on the NEW items
    returned_total = Decimal("0")
    returned_vat_base = Decimal("0")              # returned items that carried VAT
    new_total = Decimal("0")
    new_vat_base = Decimal("0")

    for it in returned:
        qty = _dec(it.get("qty"))
        if qty <= 0:
            continue
        unit_price = _dec(it.get("unit_price"))
        factor = _dec(it.get("factor"), "1")
        value = qty * unit_price
        returned_total += value
        if bool(it.get("is_vat")):
            returned_vat_base += value
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
        is_vat = vat_applied   # VAT is a whole-transaction toggle, same as a sale
        lt = qty * unit_price - discount
        if lt < 0:
            lt = Decimal("0")
        new_total += lt
        if is_vat:
            new_vat_base += lt
        product = db.get(models.Product, int(ln["product_id"])) if ln.get("product_id") else None
        if product:
            _deduct_stock(product, qty * factor)
            db.add(models.StockMovement(product_id=product.id, qty_base=-(qty * factor), reason="exchange-sale"))
        ex.lines.append(models.SaleLine(
            product_id=product.id if product else None, product_name=ln.get("name") or "Item",
            unit_name=ln.get("unit_name"), unit_factor=factor, qty=qty, unit_price=unit_price,
            discount=discount, line_total=_money(lt), is_vat=is_vat,
        ))

    # Both sides are already VAT-inclusive, so the difference is a straight
    # comparison; VAT is extracted from each side for reporting only.
    returned_vat = _vat_of(returned_vat_base)
    new_vat = _vat_of(new_vat_base)
    diff = _money(new_total - returned_total)  # >0 customer pays, <0 cash refund

    ex.subtotal = _money(new_total)
    ex.vat_amount = new_vat - returned_vat
    ex.net_amount = diff - (new_vat - returned_vat)
    ex.total = diff
    if diff > 0:
        method = (data.get("payment_method") or "cash").strip().lower()
        if method not in METHOD_LABELS or method in ("receivable", "cheque"):
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
    # Same idea as refunds: point the exchange at the invoice it came from.
    ex.invoice_no = _linked_ref(db, "EXC", orig) or f"EXC-{ex.id:06d}"
    db.commit()
    return {"ok": True, "sale_id": ex.id, "invoice_no": ex.invoice_no}


@router.get("/pos/receipt/{sale_id:int}", response_class=HTMLResponse)
def pos_receipt(
    sale_id: int,
    request: Request,
    from_: str = Query("", alias="from"),
    cust: int = 0,
    quote: int = 0,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    sale = db.get(models.Sale, sale_id)
    if not sale:
        return RedirectResponse("/pos", status_code=302)

    # Refunds/exchanges made FROM this invoice (there can be more than one —
    # e.g. two separate partial refunds over time).
    linked = (
        db.query(models.Sale)
        .filter(models.Sale.original_sale_id == sale.id)
        .order_by(models.Sale.id)
        .all()
    )
    # If this receipt IS a refund/exchange, the invoice it came from.
    original = db.get(models.Sale, sale.original_sale_id) if sale.original_sale_id else None

    # Live outstanding credit (original minus every payment collected since),
    # instead of the frozen amount recorded at the moment of sale.
    credit_paid = (
        db.query(func.coalesce(func.sum(models.ReceivableSettlement.amount), 0))
        .filter(models.ReceivableSettlement.sale_id == sale.id)
        .scalar()
    )
    credit_outstanding = (sale.receivable_amount or Decimal("0")) - Decimal(str(credit_paid or 0))

    return templates.TemplateResponse(
        "receipt.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "sale": sale, "from": from_, "cust": cust, "quote": quote,
         "linked": linked, "original": original, "credit_outstanding": credit_outstanding},
    )
