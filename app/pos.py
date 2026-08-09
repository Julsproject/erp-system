"""Point of Sale.

POS v1: search products, sell by any unit from the ladder, per-line and overall
discount, VAT (12% inclusive) computation, single payment + change, inventory
deduction in base units, printable receipt.

Deferred: customers/receivable, split payments, open-container display, returns.
"""
import json
import re
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import accounting, audit, models, settings_store
from .customers import get_or_create_customer
from .database import get_db
from .deps import get_current_user, is_staff
from .products import _get_or_create_category, _get_or_create_subcategory, _get_or_create_unit_type
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
MANILA = ZoneInfo("Asia/Manila")


def _resolve_txn_datetime(txn_date):
    """Turn an optional 'YYYY-MM-DD' transaction date into a timezone-aware
    Manila timestamp for a backdated entry. Blank/invalid -> None, which
    lets the DB stamp the live 'now' as before. The chosen date is combined
    with the current wall-clock time so several entries backdated to the
    same day still keep their encoding order, and so it converts cleanly to
    that same calendar date in every date-based report (which read in
    Asia/Manila). A future date is rejected — you can't record a sale that
    hasn't happened."""
    raw = (txn_date or "").strip()
    if not raw:
        return None, None
    try:
        d = date.fromisoformat(raw)
    except ValueError:
        return None, "Enter a valid transaction date."
    now = datetime.now(MANILA)
    if d > now.date():
        return None, "Transaction date can't be in the future."
    return now.replace(year=d.year, month=d.month, day=d.day), None


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


def _can_void_sale(user) -> bool:
    """Admin/Manager can always void; a cashier only if the owner has
    explicitly turned that on in Settings (off by default)."""
    if is_staff(user):
        return True
    return (user.role or "").lower() == "cashier" and settings_store.cashier_can_void()


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
    subcategories = db.query(models.SubCategory).order_by(models.SubCategory.name).all()
    unit_types = db.query(models.UnitType).order_by(models.UnitType.name).all()
    encoders = db.query(models.Encoder).filter(models.Encoder.is_active.is_(True)).order_by(models.Encoder.name).all()
    return templates.TemplateResponse(
        "pos.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "categories": categories, "subcategories": subcategories, "unit_types": unit_types,
         "encoders": encoders},
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
        # Same "one price stays plain, markup/margin only show up once set"
        # rule as the base unit above — an existing product with a flat
        # per-unit price still looks exactly as it did before this existed.
        if (u.price or 0) > 0 or ((u.markup_price or 0) <= 0 and (u.margin_price or 0) <= 0):
            units.append({"name": u.name, "label": u.name, "factor": float(u.factor_to_base or 1),
                          "price": float(u.price or 0), "tier": "fixed"})
        if (u.markup_price or 0) > 0:
            units.append({"name": u.name, "label": f"{u.name} · Markup", "factor": float(u.factor_to_base or 1),
                          "price": float(u.markup_price), "tier": "markup"})
        if (u.margin_price or 0) > 0:
            units.append({"name": u.name, "label": f"{u.name} · Margin", "factor": float(u.factor_to_base or 1),
                          "price": float(u.margin_price), "tier": "margin"})
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
    product.subcategory = _get_or_create_subcategory(db, data.get("subcategory"), product.category)
    product.unit_type = _get_or_create_unit_type(db, data.get("unit_type") or "Piece")
    db.add(product)
    db.commit()
    db.refresh(product)
    return {"ok": True, "existed": False, "product": _product_payload_for_pos(product)}


def _finalize_sale(db: Session, user, *, invoice_no, customer_name, vat_applied, discount_total, lines, payments, txn_date=None, receipt_type=None, encoded_by_id=None):
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

    # Optional backdating: the shop enters past transactions after the fact,
    # so a real transaction date lands the sale on the day it happened in
    # every report; blank keeps the live 'now'.
    backdated, date_err = _resolve_txn_datetime(txn_date)
    if date_err:
        return False, date_err

    # A backdated sale is a past transaction being encoded after the fact —
    # its goods already physically left the shelf, so they're already missing
    # from any count in progress. A stock count line snapshots system_qty when
    # the product is first scanned and Complete applies (counted - snapshot),
    # so encoding one now would deduct the same goods a second time and leave
    # the count short. Live sales during a count are fine (that's what the
    # delta design is for) — only backdated ones have to wait.
    if backdated:
        open_count = db.query(models.StockCount).filter(models.StockCount.status == "open").first()
        if open_count:
            return False, (
                f"Stock count {open_count.ref_no} is open. Finish (or cancel) it before encoding "
                "backdated sales — otherwise those goods get deducted twice and the count comes out short. "
                "A sale dated today still works normally."
            )

    customer_name = (customer_name or "").strip()
    vat_applied = bool(vat_applied)
    encoded_by_id = int(encoded_by_id) if encoded_by_id else None
    sale = models.Sale(
        invoice_no=invoice_no, receipt_type=(receipt_type or "").strip() or None,
        customer_name=customer_name or None, cashier_id=user.id,
        encoded_by_id=encoded_by_id,
    )
    if backdated:
        sale.created_at = backdated
    db.add(sale)

    subtotal = Decimal("0")

    for ln in lines:
        # Row-level lock: if two checkouts hit the same product at once, the
        # second one waits here until the first commits, so it always reads
        # the real post-deduction stock instead of a stale snapshot (a "lost
        # update" that could let both sales pass an insufficient-stock check
        # that only one of them should have passed).
        product = db.get(models.Product, int(ln["product_id"]), with_for_update=True) if ln.get("product_id") else None
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
        # Deliberately no insufficient-stock guard: the shop encodes a backlog
        # of past sales before its opening stock is ever loaded, so on-hand is
        # routinely 0 (or already negative) for items that genuinely sold. A
        # hard block would make that backlog impossible to enter. Stock is
        # allowed to go negative and the next Stock Count reconciles it to the
        # real shelf count — see _deduct_stock, and the "over stock" badge the
        # POS already shows on these lines.
        _deduct_stock(product, base_qty)
        sale_unit_cost = Decimal(str(product.cost_price or 0))
        db.add(models.StockMovement(
            product_id=product.id, qty_base=-base_qty, reason="sale",
            unit_cost=sale_unit_cost, value=-base_qty * sale_unit_cost,
        ))

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
        # Credit (and post-dated cheques) fall due after the customer's agreed
        # terms, counted from the transaction date — so a backdated credit
        # sale is already correctly aged (or overdue) instead of getting a
        # fresh clock from today.
        if receivable_amount > 0:
            days = customer.credit_days if customer.credit_days is not None else 15
            base_date = backdated.date() if backdated else date.today()
            sale.due_date = base_date + timedelta(days=int(days))

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

    # Auto-post to the accounting ledger (Phase 1: Sales only — see
    # app/accounting.py). Never blocks the sale itself: a missing/misconfigured
    # account mapping is a bookkeeping gap to fix in Accounting Setup, not a
    # reason to stop a cashier mid-checkout. /accounting/reconcile-sales is
    # exactly the tool for catching a gap like that after the fact.
    try:
        accounting.post_sale(db, sale, method_rows=method_rows, receivable_amount=receivable_amount, entered_by_id=user.id)
    except accounting.PostingError:
        pass

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
        txn_date=data.get("txn_date"),
        receipt_type=data.get("receipt_type"),
        encoded_by_id=data.get("encoded_by_id"),
    )
    if not ok:
        return JSONResponse({"ok": False, "error": result}, status_code=400)

    sale = result
    return {"ok": True, "sale_id": sale.id, "invoice_no": sale.invoice_no}


@router.get("/pos/next-invoice")
def pos_next_invoice(receipt_type: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Suggest the next invoice # for a receipt booklet (DRS/DRB/SI/...) by
    incrementing the last one used for that same type — each booklet has its
    own physical numbering, separate from the others. Returns "" when there's
    no prior invoice of that type to count from, or its number doesn't end in
    digits to increment."""
    if not user:
        return JSONResponse({"invoice_no": ""}, status_code=401)
    receipt_type = (receipt_type or "").strip()
    if not receipt_type:
        return {"invoice_no": ""}
    last = (
        db.query(models.Sale)
        .filter(models.Sale.receipt_type == receipt_type, models.Sale.txn_type == "sale")
        .order_by(models.Sale.id.desc())
        .first()
    )
    if not last or not last.invoice_no:
        return {"invoice_no": ""}
    m = re.match(r"^(.*?)(\d+)$", last.invoice_no)
    if not m:
        return {"invoice_no": ""}
    prefix, digits = m.group(1), m.group(2)
    next_no = str(int(digits) + 1).zfill(len(digits))
    return {"invoice_no": f"{prefix}{next_no}"}


@router.get("/pos/lookup")
def pos_lookup(invoice: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Find an original SALE by invoice number, for refund/exchange."""
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    invoice = (invoice or "").strip()
    sale = (
        db.query(models.Sale)
        .filter(models.Sale.invoice_no == invoice, models.Sale.txn_type == "sale", models.Sale.is_voided.is_(False))
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
        receipt_type=(data.get("receipt_type") or "").strip() or None,
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
        product = db.get(models.Product, int(it["product_id"]), with_for_update=True) if it.get("product_id") else None
        if product:
            _add_stock(product, qty * factor)
            refund_unit_cost = Decimal(str(product.cost_price or 0))
            db.add(models.StockMovement(
                product_id=product.id, qty_base=qty * factor, reason="refund",
                unit_cost=refund_unit_cost, value=qty * factor * refund_unit_cost,
            ))
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
        receipt_type=(data.get("receipt_type") or "").strip() or None,
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
        product = db.get(models.Product, int(it["product_id"]), with_for_update=True) if it.get("product_id") else None
        if product:
            _add_stock(product, qty * factor)
            ex_return_cost = Decimal(str(product.cost_price or 0))
            db.add(models.StockMovement(
                product_id=product.id, qty_base=qty * factor, reason="exchange-return",
                unit_cost=ex_return_cost, value=qty * factor * ex_return_cost,
            ))
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
        product = db.get(models.Product, int(ln["product_id"]), with_for_update=True) if ln.get("product_id") else None
        if product:
            base_qty = qty * factor
            _deduct_stock(product, base_qty)  # oversell allowed — see _finalize_sale
            ex_sale_cost = Decimal(str(product.cost_price or 0))
            db.add(models.StockMovement(
                product_id=product.id, qty_base=-base_qty, reason="exchange-sale",
                unit_cost=ex_sale_cost, value=-base_qty * ex_sale_cost,
            ))
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
    void_error: str = "",
    edit_date_error: str = "",
    edit_items_error: str = "",
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
         "linked": linked, "original": original, "credit_outstanding": credit_outstanding,
         "can_void": _can_void_sale(user), "void_error": VOID_ERRORS.get(void_error),
         "can_edit_date": is_staff(user), "edit_date_error": EDIT_DATE_ERRORS.get(edit_date_error),
         "today_iso": datetime.now(MANILA).date().isoformat(),
         "can_edit_items": is_staff(user) and _can_edit_sale_items(db, sale) is None,
         "edit_items_error": EDIT_ITEMS_ERRORS.get(edit_items_error)},
    )


# Phase 1 of Void a Sale: only a clean, standalone "sale" can be voided —
# nothing with credit, a linked refund/exchange, or a post-dated cheque
# already touching it. Those cases involve money that's moved somewhere
# else and need to be unwound by hand first; this covers the actual everyday
# case (a same-day data-entry typo caught before anything else built on it).
VOID_ERRORS = {
    "reason": "Enter a reason for voiding this sale.",
    "voided": "This sale is already voided.",
    "type": "Only a plain sale can be voided here — not a refund or exchange.",
    "credit": "This sale has a credit balance — settle or write it off before voiding.",
    "linked": "This sale has a refund or exchange linked to it — void that first.",
    "pdc": "This sale has a post-dated cheque recorded against it.",
    "denied": "You don't have permission to void sales.",
}

EDIT_DATE_ERRORS = {
    "denied": "You don't have permission to edit a sale's transaction date.",
    "voided": "This sale is voided — its date can't be edited.",
    "invalid": "Enter a valid date.",
    "future": "Transaction date can't be in the future.",
}

# Editing a sale's items in place is only offered for the simple, common
# case — a same-day walk-in mistake caught before anything else built on
# it. Anything involving credit, a cheque, a split payment, or a linked
# refund/exchange has to go through Void instead: those cases have money or
# stock already moved somewhere an in-place edit can't safely follow.
EDIT_ITEMS_ERRORS = {
    "denied": "You don't have permission to edit a sale's items.",
    "voided": "This sale is voided.",
    "type": "Only a plain sale can be edited here — not a refund or exchange.",
    "credit": "This sale has a credit or cheque payment — void it instead of editing.",
    "split": "This sale was paid with more than one payment method — void it instead of editing.",
    "linked": "This sale has a refund or exchange linked to it — void that first.",
    "pdc": "This sale has a post-dated cheque recorded against it.",
    "empty": "A sale needs at least one item.",
}


def _can_edit_sale_items(db: Session, sale: models.Sale):
    """Returns an EDIT_ITEMS_ERRORS key, or None if this sale is eligible for
    in-place item editing. Same underlying restrictions as void (see
    void_sale) — an edit is really "reverse the old lines, apply new ones to
    the same invoice" under the hood, so anywhere void wouldn't be safe,
    editing isn't either."""
    if sale.is_voided:
        return "voided"
    if sale.txn_type != "sale":
        return "type"
    if (sale.receivable_amount or 0) > 0:
        return "credit"
    if len(sale.payments) != 1:
        return "split"
    if db.query(models.Sale.id).filter(models.Sale.original_sale_id == sale.id).first():
        return "linked"
    if db.query(models.PostDatedCheque.id).filter(models.PostDatedCheque.sale_id == sale.id).first():
        return "pdc"
    return None


@router.post("/pos/receipt/{sale_id:int}/void")
def void_sale(
    sale_id: int,
    request: Request,
    reason: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    sale = db.get(models.Sale, sale_id)
    if not sale:
        return RedirectResponse("/pos", status_code=302)

    def _back(err=None):
        suffix = f"&void_error={err}" if err else ""
        return RedirectResponse(f"/pos/receipt/{sale_id}?from=sales{suffix}", status_code=302)

    if not _can_void_sale(user):
        return _back("denied")
    if sale.is_voided:
        return _back("voided")
    if sale.txn_type != "sale":
        return _back("type")
    reason = (reason or "").strip()
    if not reason:
        return _back("reason")
    if (sale.receivable_amount or 0) > 0:
        return _back("credit")
    linked_exists = db.query(models.Sale.id).filter(models.Sale.original_sale_id == sale.id).first()
    if linked_exists:
        return _back("linked")
    pdc_exists = db.query(models.PostDatedCheque.id).filter(models.PostDatedCheque.sale_id == sale.id).first()
    if pdc_exists:
        return _back("pdc")

    for line in sale.lines:
        if not line.product_id:
            continue
        product = db.get(models.Product, line.product_id, with_for_update=True)
        if not product:
            continue
        base_qty = Decimal(str(line.qty or 0)) * Decimal(str(line.unit_factor or 1))
        _add_stock(product, base_qty)
        unit_cost = Decimal(str(line.unit_cost or 0))
        db.add(models.StockMovement(
            product_id=product.id, qty_base=base_qty, reason="void",
            ref=sale.invoice_no, unit_cost=unit_cost, value=base_qty * unit_cost,
            note=f"Void: {reason}",
        ))

    # No settlements/PDC exist at this point (checked above), so any Payment
    # rows here were plain cash/gcash/card/etc. for this sale alone — remove
    # them so cashier-shift and payment-method totals don't still count money
    # attributed to a sale that no longer counts as having happened.
    db.query(models.Payment).filter(models.Payment.sale_id == sale.id).delete(synchronize_session=False)

    sale.is_voided = True
    sale.void_reason = reason[:255]
    sale.voided_at = datetime.now(MANILA)
    sale.voided_by_id = user.id

    # Reverse whatever was posted to the ledger for this sale. A no-op if
    # this sale predates the accounting module (never had a journal entry).
    accounting.reverse_sale_posting(db, sale, reason=f"Voided: {reason}", entered_by_id=user.id)

    audit.record(
        db, user=user, request=request, action="void", entity_type="sale",
        entity_id=sale.id, entity_label=sale.invoice_no,
        summary=f"Voided sale {sale.invoice_no}: {reason}",
    )
    db.commit()
    return _back()


@router.post("/pos/receipt/{sale_id:int}/edit-date")
def edit_sale_date(
    sale_id: int,
    request: Request,
    new_date: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Correct the transaction date recorded on an already-saved sale — for
    fixing backlog encoding mistakes (e.g. today's date got left on a sale
    that actually happened last week). Admin/manager only: this quietly
    moves the sale between reporting periods (Sales list, Reports, Inventory
    Adjustments all key off created_at), so it isn't exposed to cashiers the
    way Void is. Only the date changes — the original time-of-day is kept,
    same as a manual DB fix would, so ordering among that day's other sales
    stays sensible."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    sale = db.get(models.Sale, sale_id)
    if not sale:
        return RedirectResponse("/pos", status_code=302)

    def _back(err=None):
        suffix = f"&edit_date_error={err}" if err else ""
        return RedirectResponse(f"/pos/receipt/{sale_id}?from=sales{suffix}", status_code=302)

    if not is_staff(user):
        return _back("denied")
    if sale.is_voided:
        return _back("voided")

    try:
        new_d = date.fromisoformat((new_date or "").strip())
    except ValueError:
        return _back("invalid")
    today = datetime.now(MANILA).date()
    if new_d > today:
        return _back("future")

    old_created_at = sale.created_at
    old_time = old_created_at.timetz() if old_created_at else datetime.now(MANILA).timetz()
    new_created_at = datetime.combine(new_d, old_time)
    if new_created_at == old_created_at:
        return _back()

    sale.created_at = new_created_at
    audit.record(
        db, user=user, request=request, action="update", entity_type="sale",
        entity_id=sale.id, entity_label=sale.invoice_no,
        summary=f"Corrected transaction date for sale {sale.invoice_no}",
        changes={"created_at": [old_created_at.isoformat() if old_created_at else None, new_created_at.isoformat()]},
    )
    db.commit()
    return _back()


@router.get("/pos/receipt/{sale_id:int}/edit", response_class=HTMLResponse)
def edit_sale_items_form(sale_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    sale = db.get(models.Sale, sale_id)
    if not sale:
        return RedirectResponse("/pos", status_code=302)
    if not is_staff(user):
        return RedirectResponse(f"/pos/receipt/{sale_id}?from=sales&edit_items_error=denied", status_code=302)
    block_reason = _can_edit_sale_items(db, sale)
    if block_reason:
        return RedirectResponse(f"/pos/receipt/{sale_id}?from=sales&edit_items_error={block_reason}", status_code=302)

    lines_payload = []
    for line in sale.lines:
        lines_payload.append({
            "product_id": line.product_id,
            "name": line.product_name,
            "unit_name": line.unit_name or "Unit",
            "factor": float(line.unit_factor or 1),
            "qty": float(line.qty or 0),
            "unit_price": float(line.unit_price or 0),
            "discount": float(line.discount or 0),
            "tier": line.price_tier or "fixed",
        })
    return templates.TemplateResponse(
        "edit_sale.html",
        {
            "request": request, "app_name": request.app.title, "user": user, "sale": sale,
            "lines_json": json.dumps(lines_payload),
            "vat_applied": bool(sale.lines and sale.lines[0].is_vat),
        },
    )


@router.post("/pos/receipt/{sale_id:int}/edit")
# TODO(accounting): this recomputes sale.total/net_amount/vat_amount after the
# original sale already posted a journal entry (see accounting.post_sale) —
# the ledger currently does NOT get a correcting entry when items are edited,
# so a sale corrected here will disagree with its own journal entry until this
# is extended to post a delta (or edits are blocked once a sale has posted).
async def edit_sale_items(sale_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    sale = db.get(models.Sale, sale_id)
    if not sale:
        return JSONResponse({"ok": False, "error": "Sale not found."}, status_code=404)
    if not is_staff(user):
        return JSONResponse({"ok": False, "error": EDIT_ITEMS_ERRORS["denied"]}, status_code=403)
    block_reason = _can_edit_sale_items(db, sale)
    if block_reason:
        return JSONResponse({"ok": False, "error": EDIT_ITEMS_ERRORS[block_reason]}, status_code=400)

    data = await request.json()
    new_lines = data.get("lines") or []
    if not new_lines:
        return JSONResponse({"ok": False, "error": EDIT_ITEMS_ERRORS["empty"]}, status_code=400)
    vat_applied = bool(data.get("vat_applied"))
    discount_total = _dec(data.get("discount_total"))

    old_total = sale.total
    old_line_count = len(sale.lines)

    # Reverse every existing line's stock effect first — same mechanics as
    # Void — but keep the original StockMovement rows as history; only add
    # compensating entries, don't delete them.
    for line in sale.lines:
        if not line.product_id:
            continue
        product = db.get(models.Product, line.product_id, with_for_update=True)
        if not product:
            continue
        base_qty = Decimal(str(line.qty or 0)) * Decimal(str(line.unit_factor or 1))
        _add_stock(product, base_qty)
        unit_cost = Decimal(str(line.unit_cost or 0))
        db.add(models.StockMovement(
            product_id=product.id, qty_base=base_qty, reason="sale-edit-reverse",
            ref=sale.invoice_no, unit_cost=unit_cost, value=base_qty * unit_cost,
            note="Reversed for item correction",
        ))
    sale.lines = []  # cascade="all, delete-orphan" removes the old rows

    subtotal = Decimal("0")
    for ln in new_lines:
        product = db.get(models.Product, int(ln["product_id"]), with_for_update=True) if ln.get("product_id") else None
        if not product:
            continue
        qty = _dec(ln.get("qty"))
        unit_price = _dec(ln.get("unit_price"))
        factor = _dec(ln.get("factor"), "1")
        discount = _dec(ln.get("discount"))

        line_total = qty * unit_price - discount
        if line_total < 0:
            line_total = Decimal("0")
        subtotal += line_total

        base_qty = qty * factor
        _deduct_stock(product, base_qty)
        unit_cost = Decimal(str(product.cost_price or 0))
        db.add(models.StockMovement(
            product_id=product.id, qty_base=-base_qty, reason="sale", ref=sale.invoice_no,
            unit_cost=unit_cost, value=-base_qty * unit_cost, note="Corrected item",
        ))
        sale.lines.append(models.SaleLine(
            product_id=product.id, product_name=product.name, unit_name=ln.get("unit_name"),
            unit_factor=factor, qty=qty, unit_price=unit_price, discount=discount,
            line_total=_money(line_total), is_vat=vat_applied,
            price_tier=(ln.get("tier") or "fixed"), unit_cost=_money(product.cost_price or 0),
        ))

    if not sale.lines:
        db.rollback()
        return JSONResponse({"ok": False, "error": "None of the items matched a real product."}, status_code=400)

    total = subtotal - discount_total
    if total < 0:
        total = Decimal("0")
    vat_amount = _vat_of(total) if vat_applied else Decimal("0")
    net = _money(total) - vat_amount

    sale.subtotal = _money(subtotal)
    sale.discount_total = _money(discount_total)
    sale.vat_amount = vat_amount
    sale.net_amount = _money(net)
    sale.total = _money(total)

    # Exactly one non-credit payment is guaranteed by _can_edit_sale_items —
    # auto-adjust it to settle the corrected total exactly (per shop's own
    # choice: this is a same-day cash-drawer correction, not a new sale).
    payment = sale.payments[0]
    payment.amount = _money(total)
    sale.amount_tendered = _money(total)
    sale.change_amount = Decimal("0")

    audit.record(
        db, user=user, request=request, action="update", entity_type="sale",
        entity_id=sale.id, entity_label=sale.invoice_no,
        summary=f"Corrected items on sale {sale.invoice_no}: {old_line_count} line(s) → {len(sale.lines)}, total {old_total:g} → {sale.total:g}",
        changes={"total": [str(old_total), str(sale.total)]},
    )
    db.commit()
    return {"ok": True, "sale_id": sale.id}


@router.get("/pos/receipt/{sale_id:int}/pdf")
def pos_receipt_pdf(sale_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """A simple downloadable PDF version of the receipt — invoice header,
    the items bought, and the total. No frills, matches what's on screen."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    sale = db.get(models.Sale, sale_id)
    if not sale:
        return RedirectResponse("/pos", status_code=302)

    import io
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from fastapi.responses import Response as FileResponse
    from .pdf_utils import letterhead

    biz = settings_store.get_all(db)
    doc_label = "Refund Slip" if sale.txn_type == "refund" else ("Exchange Slip" if sale.txn_type == "exchange" else "Sales Invoice")

    doc_meta = [
        f"Invoice #: {sale.invoice_no}",
        f"Date: {sale.created_at.strftime('%b %d, %Y %I:%M %p') if sale.created_at else ''}",
    ]
    if sale.cashier:
        doc_meta.append(f"Cashier: {sale.cashier.full_name or sale.cashier.username}")

    party_lines = [sale.customer_name or "Walk-in"]
    if sale.customer:
        if sale.customer.tin:
            party_lines.append(f"TIN {sale.customer.tin}")
        if sale.customer.address:
            party_lines.append(sale.customer.address)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=18 * mm, bottomMargin=18 * mm, leftMargin=18 * mm, rightMargin=18 * mm)
    styles = getSampleStyleSheet()
    elements = letterhead(biz, doc_label, doc_meta, "Customer", party_lines)

    table_data = [["Item", "Qty", "Unit Price", "Amount"]]
    for l in sale.lines:
        table_data.append([
            l.product_name,
            f"{l.qty} {l.unit_name or ''}".strip(),
            f"{l.unit_price:,.2f}",
            f"{l.line_total:,.2f}",
        ])
    table = Table(table_data, colWidths=[220, 90, 90, 90])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F6FEB")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 14))
    elements.append(Paragraph(f"<b>TOTAL: {sale.total:,.2f}</b>", styles["Heading3"]))

    doc.build(elements)
    buf.seek(0)
    return FileResponse(
        content=buf.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="receipt_{sale.invoice_no}.pdf"'},
    )
