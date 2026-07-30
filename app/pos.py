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
from .products import _get_or_create_category, _get_or_create_unit_type
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
    """Reduce on-hand by base_qty, taking from Actual Beginning Stock first,
    then Stocks Qty. If beginning stock is already at or below 0 (e.g. from
    a past oversell), nothing more can come from it — the full amount comes
    from Stocks Qty instead."""
    available_beginning = max(product.beginning_stock or Decimal("0"), Decimal("0"))
    take = min(base_qty, available_beginning)
    product.beginning_stock = (product.beginning_stock or Decimal("0")) - take
    remainder = base_qty - take
    if remainder > 0:
        product.stock_qty = (product.stock_qty or Decimal("0")) - remainder


def _add_stock(product: models.Product, base_qty: Decimal):
    """Add base_qty back to on-hand (used by refunds and exchange returns)."""
    product.stock_qty = (product.stock_qty or Decimal("0")) + base_qty


def _sale_outstanding(db: Session, sale) -> Decimal:
    """How much of a sale's receivable is still unpaid."""
    settled = (
        db.query(func.coalesce(func.sum(models.ReceivableSettlement.amount), 0))
        .filter(models.ReceivableSettlement.sale_id == sale.id)
        .scalar()
    )
    return (sale.receivable_amount or Decimal("0")) - Decimal(str(settled or 0))


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
def pos_page(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    categories = db.query(models.Category).order_by(models.Category.name).all()
    unit_types = db.query(models.UnitType).order_by(models.UnitType.name).all()
    return templates.TemplateResponse(
        "pos.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "categories": categories, "unit_types": unit_types},
    )


def _product_payload_for_pos(p: models.Product) -> dict:
    """Shape a product for any POS-style picker (search results, quotation
    editor, …): base unit at each of its three prices, plus its ladder units."""
    base_unit = p.unit_type.name if p.unit_type else "Unit"
    # The base unit is offered at each of the product's three prices, so the
    # cashier picks the price from the same dropdown they already use to pick
    # the unit. Markup/margin only appear once they've actually been set, so
    # products priced the old way look exactly as before.
    # `name` stays the plain unit (what's stored on the sale line); `label`
    # is what the dropdown shows; `tier` is recorded against the line.
    units = [{"name": base_unit, "label": base_unit, "factor": 1.0,
              "price": float(p.selling_price or 0), "tier": "fixed"}]
    if (p.markup_price or 0) > 0:
        units.append({"name": base_unit, "label": f"{base_unit} · Markup", "factor": 1.0,
                      "price": float(p.markup_price), "tier": "markup"})
    if (p.margin_price or 0) > 0:
        units.append({"name": base_unit, "label": f"{base_unit} · Margin", "factor": 1.0,
                      "price": float(p.margin_price), "tier": "margin"})
    for u in p.units:
        units.append({"name": u.name, "label": u.name, "factor": float(u.factor_to_base or 1),
                      "price": float(u.price or 0), "tier": ""})
    c = p.container
    container = None if not c else {
        "pack_name": c["pack_name"],
        "loose_name": c["loose_name"],
        "sealed": c["sealed"],
        "open": float(c["open"]),
    }
    return {
        "id": p.id,
        "name": p.name,
        "is_vat": bool(p.is_vat),
        "base_unit": base_unit,
        "on_hand": float((p.beginning_stock or 0) + (p.stock_qty or 0)),
        "units": units,
        "container": container,
    }


@router.get("/pos/search")
def pos_search(q: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    q = (q or "").strip()
    if q:
        # A scanned barcode is an exact, unique code — if it matches, that's
        # the product, full stop. Only fall back to a fuzzy name search when
        # nothing scans to it, so typing a product name still works as before.
        barcode_hit = (
            db.query(models.Product)
            .filter(models.Product.is_active.is_(True), models.Product.barcode == q)
            .first()
        )
        if barcode_hit:
            return {"products": [_product_payload_for_pos(barcode_hit)]}
    query = db.query(models.Product).filter(models.Product.is_active.is_(True))
    if q:
        query = query.filter(models.Product.name.ilike(f"%{q}%"))
    products = query.order_by(models.Product.name).limit(30).all()
    return {"products": [_product_payload_for_pos(p) for p in products]}


@router.get("/pos/product/{product_id:int}")
def pos_product(product_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """A single product's current units/prices — used to let an editor (e.g.
    the pending-quotation editor) offer unit/price-tier switching on a line
    that was added before this lookup existed on the page."""
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    p = db.get(models.Product, product_id)
    if not p or not p.is_active:
        return {"found": False}
    return {"found": True, "product": _product_payload_for_pos(p)}


@router.post("/pos/quick-product")
async def pos_quick_product(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Create a product that isn't in inventory yet, straight from the POS
    screen — cashiers use this too, so it deliberately has no selling-price
    fields (that's the owner's call, not shown here). It's added to the cart
    at ₱0; the cashier types whatever this specific sale charges directly on
    the cart line, same as any other manually-priced line."""
    if not user:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    data = await request.json()
    name = (data.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "Product name is required."}, status_code=400)

    existing = (
        db.query(models.Product)
        .filter(func.lower(models.Product.name) == name.lower())
        .filter(models.Product.is_active.is_(True))
        .first()
    )
    if existing:
        return {"ok": True, "existed": True, "product": _product_payload_for_pos(existing)}

    product = models.Product(
        name=name,
        cost_price=_money(data.get("cost_price") or 0),
        selling_price=Decimal("0"),
        beginning_stock=_dec(data.get("beginning_stock")),
        stock_qty=_dec(data.get("stock_qty")),
        reorder_level=_dec(data.get("reorder_level")),
        is_active=True,
    )
    product.category = _get_or_create_category(db, data.get("category"))
    product.unit_type = _get_or_create_unit_type(db, data.get("unit_type") or "Piece")
    db.add(product)
    db.commit()
    db.refresh(product)
    return {"ok": True, "existed": False, "product": _product_payload_for_pos(product)}


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
            price_tier=(ln.get("tier") or "fixed"),
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
    # What's still owed on this sale — lets the delivery screen offer COD only
    # when there is actually a balance for the driver to collect.
    paid = (
        db.query(func.coalesce(func.sum(models.ReceivableSettlement.amount), 0))
        .filter(models.ReceivableSettlement.sale_id == sale.id)
        .scalar()
    )
    outstanding = Decimal(str(sale.receivable_amount or 0)) - Decimal(str(paid or 0))
    return {
        "found": True,
        "sale_id": sale.id,
        "invoice_no": sale.invoice_no,
        "customer_name": sale.customer_name or "",
        "date": sale.created_at.strftime("%b %d, %Y %I:%M %p") if sale.created_at else "",
        "outstanding": float(outstanding if outstanding > 0 else 0),
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

    # The cashier's own invoice # (e.g. "45") wins when they typed one; only
    # auto-generate a reference (REF-45, or a sequential fallback) if they left it blank.
    typed_invoice = (data.get("invoice_no") or "").strip()
    if typed_invoice and db.query(models.Sale).filter(models.Sale.invoice_no == typed_invoice).first():
        return JSONResponse({"ok": False, "error": f"Invoice number '{typed_invoice}' is already used."}, status_code=400)

    # Same as a normal sale: a typed customer name gets its own Customer
    # record (created if new), not just free text on the receipt — otherwise
    # it can't show up in the Customers list or their purchase history.
    typed_customer_name = (data.get("customer_name") or "").strip()
    typed_customer = get_or_create_customer(db, typed_customer_name) if (not orig and typed_customer_name) else None

    refund = models.Sale(
        txn_type="refund",
        original_sale_id=orig.id if orig else None,
        customer_name=(orig.customer_name if orig else (typed_customer.name if typed_customer else None)),
        customer_id=(orig.customer_id if orig else (typed_customer.id if typed_customer else None)),
        cashier_id=user.id,
        # No matched invoice means these items — and their prices — came from
        # an inventory search the cashier typed in, not a verified original
        # sale line. Flagged so it surfaces in Notifications for a spot-check.
        no_invoice_return=(orig is None),
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

    # Which channel the money actually went out through — defaults to cash,
    # but the cashier can pick GCash/Bank Transfer/Cheque just as easily.
    # (Credit doesn't apply here — there's nothing new being sold to owe
    # against, so there's no receivable for it to sit on.)
    method = (data.get("payment_method") or "cash").strip().lower()
    if method not in ("cash", "gcash", "bank_transfer", "cheque"):
        method = "cash"

    refund.subtotal = -gross
    refund.net_amount = -(gross - vat)
    refund.vat_amount = -vat
    refund.total = -gross

    # If the original sale is still owed on (bought on credit, not yet paid
    # off), the returned goods were never actually paid for — so the refund
    # reduces that credit balance first, instead of paying cash out for
    # something no cash was ever collected for. Only whatever's left over
    # (if the return is worth more than what's still owed) pays out.
    applied_to_credit = Decimal("0")
    if orig:
        outstanding = _sale_outstanding(db, orig)
        if outstanding > 0:
            applied_to_credit = min(gross, outstanding)
    cash_out = gross - applied_to_credit

    refund.payment_method = (
        ("Applied to credit" if cash_out <= 0 else METHOD_LABELS[method] + " refund + credit applied")
        if applied_to_credit > 0 else METHOD_LABELS[method] + " refund"
    )
    refund.amount_tendered = Decimal("0")
    refund.change_amount = cash_out  # only the leftover, if any, paid out to customer
    db.flush()
    if applied_to_credit > 0:
        db.add(models.ReceivableSettlement(
            sale_id=orig.id, method="credit_note", amount=applied_to_credit,
            source_sale_id=refund.id, cashier_id=user.id,
        ))
    # Point the refund at the invoice it came from (REF-45); fall back to a
    # sequential number for refunds with no original invoice.
    refund.invoice_no = typed_invoice or _linked_ref(db, "REF", orig) or f"REF-{refund.id:06d}"
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

    typed_invoice = (data.get("invoice_no") or "").strip()
    if typed_invoice and db.query(models.Sale).filter(models.Sale.invoice_no == typed_invoice).first():
        return JSONResponse({"ok": False, "error": f"Invoice number '{typed_invoice}' is already used."}, status_code=400)

    # Same as a normal sale: a typed customer name gets its own Customer
    # record (created if new), not just free text on the receipt — otherwise
    # it can't show up in the Customers list or their purchase history.
    typed_customer_name = (data.get("customer_name") or "").strip()
    typed_customer = get_or_create_customer(db, typed_customer_name) if (not orig and typed_customer_name) else None

    ex = models.Sale(
        txn_type="exchange",
        original_sale_id=orig.id if orig else None,
        customer_name=(orig.customer_name if orig else (typed_customer.name if typed_customer else None)),
        customer_id=(orig.customer_id if orig else (typed_customer.id if typed_customer else None)),
        cashier_id=user.id,
        # See pos_refund: no matched invoice means the returned item(s) and
        # their price(s) came from an inventory search, not a verified sale
        # line — flagged for a Notifications spot-check.
        no_invoice_return=(orig is None and bool(returned)),
    )
    db.add(ex)

    vat_applied = bool(data.get("vat_applied"))   # VAT on the amount actually due, see below
    returned_total = Decimal("0")
    new_total = Decimal("0")

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
        is_vat = vat_applied   # per-line flag for reporting; see below for the actual VAT charged
        lt = qty * unit_price - discount
        if lt < 0:
            lt = Decimal("0")
        new_total += lt
        product = db.get(models.Product, int(ln["product_id"])) if ln.get("product_id") else None
        if product:
            _deduct_stock(product, qty * factor)
            db.add(models.StockMovement(product_id=product.id, qty_base=-(qty * factor), reason="exchange-sale"))
        ex.lines.append(models.SaleLine(
            product_id=product.id if product else None, product_name=ln.get("name") or "Item",
            unit_name=ln.get("unit_name"), unit_factor=factor, qty=qty, unit_price=unit_price,
            discount=discount, line_total=_money(lt), is_vat=is_vat,
            price_tier=(ln.get("tier") or "fixed"),
        ))

    # Both sides are already VAT-inclusive. VAT is extracted from the amount
    # actually changing hands now (the difference) — not from the new items'
    # total on its own — since that's the number on the receipt the customer
    # is being asked to pay (or refunded).
    diff = _money(new_total - returned_total)  # >0 customer pays, <0 cash refund
    if vat_applied:
        vat_amt = _vat_of(abs(diff))
        vat_signed = vat_amt if diff >= 0 else -vat_amt
    else:
        vat_signed = Decimal("0")

    ex.subtotal = _money(new_total)
    ex.vat_amount = vat_signed
    ex.net_amount = diff - vat_signed
    ex.total = diff
    pending_cheque = None   # filled in below if the customer owes and pays by cheque
    pending_credit_note = None   # filled in below if a refund gets applied to an old credit balance
    if diff > 0:
        # Same channels a normal sale accepts, since this is genuinely money
        # the customer still owes: Cash/GCash/Bank Transfer settle now;
        # Cheque and Credit both sit as a receivable until cleared/paid off.
        method = (data.get("payment_method") or "cash").strip().lower()
        if method not in ("cash", "gcash", "bank_transfer", "cheque", "receivable"):
            method = "cash"
        if method in ("cheque", "receivable") and not ex.customer_id:
            return JSONResponse({"ok": False, "error": "Cheque or Credit needs a customer name."}, status_code=400)

        if method == "receivable":
            ex.receivable_amount = diff
            ex.amount_tendered = Decimal("0")
            ex.change_amount = Decimal("0")
            ex.payment_method = METHOD_LABELS[method]
            customer = db.get(models.Customer, ex.customer_id)
            days = customer.credit_days if customer and customer.credit_days is not None else 15
            ex.due_date = date.today() + timedelta(days=int(days))
        elif method == "cheque":
            raw_date = (data.get("cheque_date") or "").strip()
            try:
                cheque_date = date.fromisoformat(raw_date)
            except ValueError:
                return JSONResponse({"ok": False, "error": "Enter a valid cheque date (the date printed on the cheque)."}, status_code=400)
            ex.receivable_amount = diff   # not cash in hand until it clears — see _finalize_sale
            ex.amount_tendered = Decimal("0")
            ex.change_amount = Decimal("0")
            ex.payment_method = METHOD_LABELS[method]
            pending_cheque = {
                "amount": diff, "cheque_date": cheque_date,
                "bank": (data.get("bank") or "").strip() or None,
                "cheque_no": (data.get("cheque_no") or "").strip() or None,
            }
        else:
            # Cash is the only method where "handed over more than owed, give
            # change back" is a real physical thing — GCash/Bank Transfer are
            # exact-amount transfers, so those still just charge the difference.
            if method == "cash":
                # Amount received is optional — just for change. Left blank or
                # under what's owed, assume they were handed exactly the amount due.
                tendered = _dec(data.get("amount_tendered"))
                if tendered < diff:
                    tendered = diff
            else:
                tendered = diff
            change = _money(tendered - diff)
            ex.payments.append(models.Payment(method=method, amount=_money(tendered)))
            ex.amount_tendered = _money(tendered)
            ex.change_amount = change
            ex.payment_method = METHOD_LABELS[method]
    elif diff < 0:
        # Money going back OUT to the customer — unless the original sale
        # is still owed on (bought on credit, unpaid), in which case the
        # returned goods were never actually paid for, so this reduces that
        # credit balance first instead of paying cash for it. Only whatever's
        # left over (return worth more than what's still owed) pays out.
        method = (data.get("payment_method") or "cash").strip().lower()
        if method not in ("cash", "gcash", "bank_transfer", "cheque"):
            method = "cash"
        gross = -diff
        applied_to_credit = Decimal("0")
        if orig:
            outstanding = _sale_outstanding(db, orig)
            if outstanding > 0:
                applied_to_credit = min(gross, outstanding)
        cash_out = gross - applied_to_credit

        ex.amount_tendered = Decimal("0")
        ex.change_amount = cash_out  # only the leftover, if any, paid out to customer
        ex.payment_method = (
            ("Applied to credit" if cash_out <= 0 else METHOD_LABELS[method] + " refund + credit applied")
            if applied_to_credit > 0 else METHOD_LABELS[method] + " refund"
        )
        if applied_to_credit > 0:
            pending_credit_note = applied_to_credit
    else:
        ex.payment_method = "Even exchange"

    db.flush()
    # Same idea as refunds: point the exchange at the invoice it came from.
    ex.invoice_no = typed_invoice or _linked_ref(db, "EXC", orig) or f"EXC-{ex.id:06d}"
    if pending_cheque:
        db.add(models.PostDatedCheque(
            direction="received", amount=_money(pending_cheque["amount"]),
            bank=pending_cheque["bank"], cheque_no=pending_cheque["cheque_no"],
            cheque_date=pending_cheque["cheque_date"],
            sale_id=ex.id, customer_id=ex.customer_id, created_by=user.id,
        ))
    if pending_credit_note:
        db.add(models.ReceivableSettlement(
            sale_id=orig.id, method="credit_note", amount=pending_credit_note,
            source_sale_id=ex.id, cashier_id=user.id,
        ))
    db.commit()
    return {"ok": True, "sale_id": ex.id, "invoice_no": ex.invoice_no}


@router.get("/pos/receipt/{sale_id:int}", response_class=HTMLResponse)
def pos_receipt(
    sale_id: int,
    request: Request,
    from_: str = Query("", alias="from"),
    cust: int = 0,
    quote: int = 0,
    thermal: int = 0,
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
         "sale": sale, "from": from_, "cust": cust, "quote": quote, "thermal": thermal,
         "linked": linked, "original": original, "credit_outstanding": credit_outstanding},
    )
