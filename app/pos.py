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
from sqlalchemy import func, or_
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
    "maya": "Maya",
    "other_ewallet": "Other E-Wallet",
    "card": "Card",
    "bank_transfer": "Bank Transfer",
    "cheque": "Cheque",
    "receivable": "Receivable",
}
# Payment methods a completed sale's method can be *corrected* to. Cheque and
# Receivable are deliberately excluded — switching TO either would need to
# open a PDC / start a credit balance from scratch, which isn't a same-day
# correction anymore; void and re-ring the sale instead for those.
EDIT_PAYMENT_METHODS = ["cash", "gcash", "maya", "other_ewallet", "card", "bank_transfer"]

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


def _display_invoice(sale: models.Sale) -> str:
    """Receipt-type-prefixed invoice # (e.g. "DRB51380", not "51380") — same
    format shown on the printed receipt, used as the Stock Card's reference
    for every sale-driven StockMovement so it reads like the paperwork."""
    return f"{sale.receipt_type}{sale.invoice_no}" if sale.receipt_type else sale.invoice_no


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


def _apply_stock_count_correction(product: models.Product, variance: Decimal):
    """Apply a Stock Count's counted-vs-system variance to on-hand, using the
    same "Actual Beginning first" rule sales already follow via _deduct_stock
    — a shortfall (counted < system) drains Beginning first, floored at 0; a
    surplus (counted > system) heals a negative Beginning back toward 0
    first, before the remainder goes to Stocks Qty.

    Without this, a Beginning that had already gone negative (most commonly
    from Month-End Rollover folding a negative Stocks Qty into it — see
    _run_month_end_rollover) could never be corrected: a count used to only
    ever touch Stocks Qty, so a negative Beginning just sat there permanently
    once created, no matter how many counts happened afterward."""
    if variance < 0:
        _deduct_stock(product, -variance)
    elif variance > 0:
        beginning = product.beginning_stock or Decimal("0")
        deficit = max(-beginning, Decimal("0"))
        heal = min(variance, deficit)
        product.beginning_stock = beginning + heal
        remainder = variance - heal
        if remainder > 0:
            product.stock_qty = (product.stock_qty or Decimal("0")) + remainder


def _find_backdated_stock_conflicts(db: Session, backdated, product_ids: list):
    """For a backdated transaction, which of these products were already
    physically counted in a Stock Count whose count_date (the date the
    physical count actually happened, not whenever it got marked done in
    the system — see that column's comment on the model) is on or after the
    backdated date — meaning that count's number may already reflect this
    item's absence, and deducting it again here would double it.

    This can't be reconstructed reliably after the fact (backdating
    overwrites the only timestamp a Sale has, so there's no way to later
    tell "entered before/after that count" apart from "dated before/after
    it" — see the Activity Log entry _finalize_sale adds when this fires,
    which IS reliable since it's written with the real clock at the moment
    of entry). This helper is only ever meant to be called right at entry
    time, either to warn the person entering it (POS's pre-checkout check)
    or to log what was true at that moment (_finalize_sale) — never as a
    retroactive scan over old data."""
    if not backdated or not product_ids:
        return []
    backdated_date = backdated.date() if hasattr(backdated, "date") else backdated
    rows = (
        db.query(models.Product.id, models.Product.name, models.StockCount.ref_no, models.StockCount.count_date)
        .join(models.StockCountLine, models.StockCountLine.product_id == models.Product.id)
        .join(models.StockCount, models.StockCount.id == models.StockCountLine.stock_count_id)
        .filter(
            models.Product.id.in_(product_ids),
            models.StockCount.status == "completed",
            models.StockCount.count_date.isnot(None),
            models.StockCount.count_date >= backdated_date,
        )
        .order_by(models.StockCount.count_date.desc())
        .all()
    )
    seen = {}
    for pid, name, ref_no, count_date in rows:
        if pid not in seen:  # most recent count per product only (query is already sorted desc)
            seen[pid] = {"product_id": pid, "name": name, "count_ref": ref_no, "count_date": count_date}
    return list(seen.values())


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


# A cashier who parks sale after sale without ever finishing them would grow
# this list forever; a cap keeps the picker usable and is far more than the
# handful genuinely in flight at once.
MAX_SALE_DRAFTS = 30

# How far past a cashier's last number to look for a free one before giving up
# and letting them type it themselves. Generous enough to step over a long run
# another till already used, small enough not to scan a whole booklet.
NEXT_INVOICE_SCAN_LIMIT = 500


def _draft_row(d: models.SaleDraft) -> dict:
    return {
        "id": d.id,
        "label": d.label or "Untitled",
        "item_count": d.item_count or 0,
        "total": float(d.total or 0),
        "saved_at": d.updated_at.isoformat() if d.updated_at else None,
    }


@router.get("/pos/drafts")
def list_sale_drafts(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """This cashier's parked sales. Scoped to the person who saved them —
    two cashiers sharing one till shouldn't be picking through each other's
    half-typed carts."""
    if not user:
        return JSONResponse({"drafts": []}, status_code=401)
    drafts = (
        db.query(models.SaleDraft)
        .filter(models.SaleDraft.created_by == user.id)
        .order_by(models.SaleDraft.updated_at.desc())
        .all()
    )
    return {"drafts": [_draft_row(d) for d in drafts]}


@router.post("/pos/drafts")
def save_sale_draft(data: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Park the POS screen's current state so the till can be freed up.

    Nothing is validated the way a real checkout is — that's the point: a
    draft exists precisely because something isn't resolved yet (an unknown
    price, an item not on file). It gets checked properly when it's resumed
    and completed as a normal sale.
    """
    if not user:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    payload = data.get("payload")
    if not isinstance(payload, dict) or not (payload.get("cart") or []):
        return JSONResponse({"ok": False, "error": "Nothing to save — add at least one item first."}, status_code=400)

    draft_id = data.get("draft_id")
    draft = None
    if draft_id:
        draft = (
            db.query(models.SaleDraft)
            .filter(models.SaleDraft.id == int(draft_id), models.SaleDraft.created_by == user.id)
            .first()
        )
    if draft is None:
        existing = (
            db.query(models.SaleDraft).filter(models.SaleDraft.created_by == user.id).count()
        )
        if existing >= MAX_SALE_DRAFTS:
            return JSONResponse(
                {"ok": False, "error": f"You already have {MAX_SALE_DRAFTS} saved drafts. Finish or delete one first."},
                status_code=400,
            )
        draft = models.SaleDraft(created_by=user.id)
        db.add(draft)

    draft.label = (data.get("label") or "").strip()[:120] or None
    draft.payload = json.dumps(payload)
    draft.item_count = len(payload.get("cart") or [])
    try:
        draft.total = Decimal(str(data.get("total") or 0))
    except (InvalidOperation, TypeError, ValueError):
        draft.total = Decimal("0")
    db.commit()
    return {"ok": True, "draft": _draft_row(draft)}


@router.get("/pos/drafts/{draft_id:int}")
def get_sale_draft(draft_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return JSONResponse({"found": False}, status_code=401)
    draft = (
        db.query(models.SaleDraft)
        .filter(models.SaleDraft.id == draft_id, models.SaleDraft.created_by == user.id)
        .first()
    )
    if not draft:
        return {"found": False}
    try:
        payload = json.loads(draft.payload)
    except ValueError:
        return {"found": False}
    return {"found": True, "id": draft.id, "label": draft.label, "payload": payload}


@router.post("/pos/drafts/{draft_id:int}/delete")
def delete_sale_draft(draft_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    draft = (
        db.query(models.SaleDraft)
        .filter(models.SaleDraft.id == draft_id, models.SaleDraft.created_by == user.id)
        .first()
    )
    if draft:
        db.delete(draft)
        db.commit()
    return {"ok": True}


def _price_term(term: str):
    """A search word read as a peso amount, or None when it isn't one.

    Customers quote the price far more often than the exact product name
    ("yung one-thirty na pintura"), so a number typed into POS search should
    be able to find what costs that much. Anything that isn't cleanly a
    positive number — "1/4", "60ML", "BS1400" — comes back None and is left
    to match names only.
    """
    try:
        value = Decimal(term.replace(",", ""))
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    return value if value > 0 else None


def _invoice_taken(db: Session, invoice_no: str, receipt_type, exclude_sale_id: int = None) -> bool:
    """Is this invoice # already used IN THIS BOOKLET?

    Each booklet (DRS/DRB/SI/...) is its own physical pad with its own
    numbering, so DRS 255 and DRB 255 are two different receipts and both
    have to be enterable — only a repeat within the same booklet is a real
    collision. The database enforces the same pairing (see migration 0057),
    so two tills saving at once still can't slip a duplicate through; this
    check exists to fail with a readable message instead of an integrity
    error.
    """
    rt = (receipt_type or "").strip() or None
    query = db.query(models.Sale.id).filter(
        models.Sale.invoice_no == invoice_no,
        models.Sale.receipt_type.is_(None) if rt is None else models.Sale.receipt_type == rt,
    )
    if exclude_sale_id:
        query = query.filter(models.Sale.id != exclude_sale_id)
    return query.first() is not None


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
        # Every word has to appear somewhere in the name, in any order —
        # matching the whole phrase as one string means "a/c seinna" finds
        # nothing, because the real name has "1/4 LITER BURNT" sitting
        # between those two words. Cashiers type the bits they remember, not
        # the full name in order.
        for term in q.split():
            name_match = models.Product.name.ilike(f"%{term}%")
            price = _price_term(term)
            if price is None:
                query = query.filter(name_match)
                continue
            # A number could be part of a name ("60ML", "BS1400") or the price
            # the customer was quoted, and there's no telling which from the
            # word alone — so match either. Checked against the unit ladder
            # too: an item bought by the bag is remembered at its bag price,
            # not the per-kilo one the base price holds.
            unit_priced = (
                db.query(models.ProductUnit.id)
                .filter(
                    models.ProductUnit.product_id == models.Product.id,
                    models.ProductUnit.price == price,
                )
                .exists()
            )
            query = query.filter(or_(name_match, models.Product.selling_price == price, unit_priced))
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
def pos_quick_product(data: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Create a product that isn't in inventory yet, straight from the POS
    screen — cashiers use this too, so it deliberately has no selling-price
    fields (that's the owner's call, not shown here). It's added to the cart
    at ₱0; the cashier types whatever this specific sale charges directly on
    the cart line, same as any other manually-priced line."""
    if not user:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

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


def _finalize_sale(db: Session, user, *, invoice_no, customer_name, vat_applied, discount_total, lines, payments, txn_date=None, receipt_type=None, encoded_by_id=None, delivery_address=None, notes=None, force_stock_deduction=False):
    """Create and commit a real Sale from line items + payments.

    Shared by POS checkout and by quotations converting to a paid sale, so the
    stock/cost/VAT/receivable math only lives in one place.
    Returns (True, sale) on success, or (False, error_message) on failure.
    """
    invoice_no = (invoice_no or "").strip()
    if not invoice_no:
        return False, "Invoice number is required."
    if _invoice_taken(db, invoice_no, receipt_type):
        booklet = (receipt_type or "").strip()
        return False, (
            f"Invoice number '{invoice_no}' is already used"
            + (f" in the {booklet} booklet." if booklet else ".")
        )

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

    # Which of this sale's products were already physically counted in a
    # completed Stock Count that covers this backdated date — see
    # _find_backdated_stock_conflicts. Their stock effect is skipped below by
    # default (that count's number already reflects them being gone; a sale
    # dated after the count deducts completely normally, same as always).
    # force_stock_deduction is an admin/manager-only escape hatch for the
    # rare case someone genuinely needs it deducted anyway — checked here,
    # not trusted from the caller, since a plain cashier could otherwise pass
    # it straight through the API.
    conflicting_ids = set()
    if backdated:
        conflicts = _find_backdated_stock_conflicts(
            db, backdated, [int(ln["product_id"]) for ln in lines if ln.get("product_id")]
        )
        conflicting_ids = {c["product_id"] for c in conflicts}
    force_stock_deduction = bool(force_stock_deduction) and is_staff(user)

    customer_name = (customer_name or "").strip()
    vat_applied = bool(vat_applied)
    encoded_by_id = int(encoded_by_id) if encoded_by_id else None
    sale = models.Sale(
        invoice_no=invoice_no, receipt_type=(receipt_type or "").strip() or None,
        customer_name=customer_name or None, cashier_id=user.id,
        encoded_by_id=encoded_by_id,
        delivery_address=(delivery_address or "").strip() or None,
        notes=(notes or "").strip() or None,
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
        sale_unit_cost = Decimal(str(product.cost_price or 0))
        if product.id in conflicting_ids and not force_stock_deduction:
            # Already physically counted in a completed Stock Count that
            # covers this date — that count's number already reflects this
            # item being gone, so deducting again here would double it.
            # No stock effect; the sale itself is still recorded normally
            # (see the audit entry added below for the full detail).
            db.add(models.StockMovement(
                product_id=product.id, qty_base=Decimal("0"), reason="sale",
                unit_cost=sale_unit_cost, value=Decimal("0"), ref=_display_invoice(sale),
                note="No stock effect — already reflected in a stock count covering this sale's date.",
            ))
        else:
            # Deliberately no insufficient-stock guard: the shop encodes a backlog
            # of past sales before its opening stock is ever loaded, so on-hand is
            # routinely 0 (or already negative) for items that genuinely sold. A
            # hard block would make that backlog impossible to enter. Stock is
            # allowed to go negative and the next Stock Count reconciles it to the
            # real shelf count — see _deduct_stock, and the "over stock" badge the
            # POS already shows on these lines.
            _deduct_stock(product, base_qty)
            db.add(models.StockMovement(
                product_id=product.id, qty_base=-base_qty, reason="sale",
                unit_cost=sale_unit_cost, value=-base_qty * sale_unit_cost, ref=_display_invoice(sale),
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

    # Logged here (with the real clock, not the sale's possibly-backdated
    # date) so it's a reliable trail for /reports/backdated-conflicts to
    # review later — same conflicts computed above, reused rather than
    # queried again, so this always matches what actually happened to stock.
    if conflicting_ids:
        skipped = not force_stock_deduction
        audit.record(
            db, user=user, action="stock_conflict", entity_type="sale",
            entity_id=sale.id, entity_label=sale.invoice_no,
            summary=(
                f"Backdated sale {sale.invoice_no}: {len(conflicts)} item(s) already counted in a stock "
                f"count covering this date — stock effect was "
                + ("skipped for those item(s) automatically." if skipped
                   else f"force-deducted anyway by {user.username} (admin override).")
            ),
            # Structured, not squeezed into the 300-char summary — the
            # report table reads this directly instead of parsing text.
            changes={
                "conflicts": [
                    {"product_id": c["product_id"], "product": c["name"], "count_ref": c["count_ref"]}
                    for c in conflicts
                ],
                "stock_effect": "forced" if force_stock_deduction else "skipped",
            },
        )

    for row in cheque_rows:
        pdc = models.PostDatedCheque(
            direction="received", amount=_money(row["amount"]),
            bank=row["bank"], cheque_no=row["cheque_no"], cheque_date=row["cheque_date"],
            sale_id=sale.id, customer_id=sale.customer_id,
            created_by=user.id,
        )
        db.add(pdc)
        db.flush()
        db.add(models.PdcApplication(pdc_id=pdc.id, sale_id=sale.id, amount=_money(row["amount"])))

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


@router.get("/pos/backdated-conflicts")
def pos_backdated_conflicts(
    txn_date: str = "", product_ids: str = "", db: Session = Depends(get_db), user=Depends(get_current_user),
):
    """Pre-checkout check for the POS "heads up" dialog — see
    _find_backdated_stock_conflicts. product_ids is a comma-separated list
    (from the cart currently on screen)."""
    if not user:
        return JSONResponse({"conflicts": []}, status_code=401)
    backdated, err = _resolve_txn_datetime(txn_date)
    if err or not backdated:
        return {"conflicts": []}
    try:
        ids = [int(x) for x in product_ids.split(",") if x.strip()]
    except ValueError:
        return {"conflicts": []}
    conflicts = _find_backdated_stock_conflicts(db, backdated, ids)
    return {
        "conflicts": [
            {
                "product_id": c["product_id"], "name": c["name"], "count_ref": c["count_ref"],
                "count_date": c["count_date"].strftime("%b %d, %Y") if c["count_date"] else "",
            }
            for c in conflicts
        ]
    }


@router.post("/pos/checkout")
def pos_checkout(data: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

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
        delivery_address=data.get("delivery_address"),
        notes=data.get("notes"),
        force_stock_deduction=data.get("force_stock_deduction"),
    )
    if not ok:
        return JSONResponse({"ok": False, "error": result}, status_code=400)

    sale = result
    return {"ok": True, "sale_id": sale.id, "invoice_no": sale.invoice_no}


def _last_receipt_number(receipt_type: str, invoice_no: str):
    """(prefix, digits-as-int, width) parsed from an invoice #, or None if it
    doesn't end in digits."""
    if not invoice_no:
        return None
    m = re.match(r"^(.*?)(\d+)$", invoice_no)
    if not m:
        return None
    return m.group(1), int(m.group(2)), len(m.group(2))


@router.get("/pos/next-invoice")
def pos_next_invoice(
    receipt_type: str = "", encoded_by_id: int = 0,
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    """Suggest the next invoice # for a receipt booklet (DRS/DRB/SI/...) by
    incrementing the last one used for that same type by THIS cashier —
    either by a real sale OR logged as cancelled/spoiled (see
    /pos/cancelled-receipts), so a torn-out receipt doesn't get its number
    silently reissued. Scoped per cashier because each cashier physically
    holds their own booklet — two cashiers can both be on a "DRS" booklet
    at completely different number ranges, so suggesting off the single
    most-recent sale system-wide (regardless of who made it) would hand
    cashier A a number from cashier B's booklet.

    A shared login (e.g. one "admin" account several people actually use)
    breaks that — every sale carries the same cashier_id no matter who's
    really at the till, so "my own last invoice" collapses back into
    "whoever on this login sold most recently." The POS's own "Encoded by"
    picker exists for exactly this case, so when it's set, that identity
    wins over the logged-in account for this suggestion.

    Returns "" when there's nothing of that type from this cashier/encoder
    to count from, or its number doesn't end in digits to increment."""
    if not user:
        return JSONResponse({"invoice_no": ""}, status_code=401)
    receipt_type = (receipt_type or "").strip()
    if not receipt_type:
        return {"invoice_no": ""}
    sale_query = db.query(models.Sale).filter(
        models.Sale.receipt_type == receipt_type, models.Sale.txn_type == "sale",
    )
    if encoded_by_id:
        sale_query = sale_query.filter(models.Sale.encoded_by_id == encoded_by_id)
    else:
        sale_query = sale_query.filter(models.Sale.cashier_id == user.id)
    last_sale = sale_query.order_by(models.Sale.id.desc()).first()
    last_cancelled = (
        db.query(models.CancelledReceipt)
        .filter(models.CancelledReceipt.receipt_type == receipt_type, models.CancelledReceipt.recorded_by_id == user.id)
        .order_by(models.CancelledReceipt.id.desc())
        .first()
    )
    # Continue from this cashier's own last SALE. Their last cancelled receipt
    # only stands in when they have no sale in this booklet yet — taking
    # whichever number is HIGHEST (the old behaviour) meant one receipt
    # spoiled far ahead dragged every later suggestion up with it, skipping
    # every good number in between.
    parsed = _last_receipt_number(receipt_type, last_sale.invoice_no if last_sale else None)
    if not parsed:
        parsed = _last_receipt_number(receipt_type, last_cancelled.invoice_no if last_cancelled else None)
    if not parsed:
        return {"invoice_no": ""}
    prefix, number, width = parsed

    # Numbers already spent in this booklet — by ANYONE, and whether they
    # became a sale or were logged as spoiled. Deliberately not scoped to this
    # cashier: a booklet number is physically unique, it can only be written
    # once no matter who was holding the pad, and saving a repeat is refused
    # anyway (see _invoice_taken).
    taken = {n for (n,) in db.query(models.Sale.invoice_no)
             .filter(models.Sale.receipt_type == receipt_type).all() if n}
    taken |= {n for (n,) in db.query(models.CancelledReceipt.invoice_no)
              .filter(models.CancelledReceipt.receipt_type == receipt_type).all() if n}

    # Step forward one at a time to the first free number, so a gap left by
    # someone else's sale is stepped over without abandoning this cashier's
    # own place in the pad. Bounded so an exhausted range can't spin.
    candidate = number + 1
    for _ in range(NEXT_INVOICE_SCAN_LIMIT):
        suggestion = f"{prefix}{str(candidate).zfill(width)}"
        if suggestion not in taken:
            return {"invoice_no": suggestion}
        candidate += 1
    return {"invoice_no": ""}


RECEIPT_TYPES = ["DRS", "DRB", "SI"]

# A booklet handed a genuinely huge range (a typo, or "just show me
# everything") would mean building and scanning that many numbers per
# request — this caps it so the page fails fast with a clear message
# instead of hanging.
INVOICE_GAP_RANGE_LIMIT = 5000


def _numeric_core(invoice_no: str):
    """The first run of digits in an invoice #, as an int — or None.

    A booklet number doesn't always come back as a clean "52259"; a
    correction or a manual entry can leave it as "OS61763P" or "61764P".
    The physical number is what a gap check actually cares about, so this
    reads through whatever prefix/suffix got typed around it rather than
    requiring an exact format match.
    """
    if not invoice_no:
        return None
    m = re.search(r"\d+", invoice_no)
    return int(m.group()) if m else None


def _invoice_gap_check(db: Session, receipt_type: str, lo: int, hi: int, width: int):
    """Numbers in [lo, hi] for one booklet, sorted into three piles: sold
    (a real sale/refund/exchange), cancelled (logged as spoiled — carried
    with its reason/date so that side of the check is actually visible, not
    just trusted), and missing (neither)."""
    sold = set()
    for (n,) in db.query(models.Sale.invoice_no).filter(models.Sale.receipt_type == receipt_type):
        core = _numeric_core(n)
        if core is not None:
            sold.add(core)

    cancelled_detail = {}  # number -> the CancelledReceipt row explaining it
    for row in db.query(models.CancelledReceipt).filter(models.CancelledReceipt.receipt_type == receipt_type):
        core = _numeric_core(row.invoice_no)
        if core is not None:
            cancelled_detail[core] = row

    checked_count = hi - lo + 1
    missing_nums = []
    cancelled_in_range = []
    for n in range(lo, hi + 1):
        if n in sold:
            continue
        if n in cancelled_detail:
            row = cancelled_detail[n]
            cancelled_in_range.append({
                "number": str(n).zfill(width),
                "date": row.cancelled_date.strftime("%b %d, %Y") if row.cancelled_date else "",
                "reason": row.reason or "",
            })
            continue
        missing_nums.append(n)

    return {
        "receipt_type": receipt_type,
        "checked_count": checked_count,
        "accounted_count": checked_count - len(missing_nums),
        "missing_count": len(missing_nums),
        "missing": [str(n).zfill(width) for n in missing_nums],
        "cancelled_in_range": cancelled_in_range,
    }


@router.get("/pos/invoice-gaps", response_class=HTMLResponse)
def invoice_gaps(
    request: Request, receipt_types: list[str] = Query(default=[]), range_from: str = "", range_to: str = "",
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    """Which numbers in a booklet's range are unaccounted for — not a real
    sale/refund/exchange AND not logged as cancelled/spoiled. The BIR-style
    check ("every number issued either sold or explained") run over a range
    instead of one at a time. Runs against each booklet picked, since a
    number range doesn't mean anything shared across booklets — DRS 52260
    and DRB 52260 are two different physical receipts with their own history.
    """
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)

    picked = [rt.strip().upper() for rt in receipt_types if rt.strip()]
    picked = [rt for rt in RECEIPT_TYPES if rt in picked]  # de-dupe, keep RECEIPT_TYPES' order
    range_from = (range_from or "").strip()
    range_to = (range_to or "").strip()
    error = None
    results = []
    total_missing = 0

    if picked and range_from and range_to:
        if not (range_from.isdigit() and range_to.isdigit()):
            error = "From and To must be plain numbers, e.g. 34451."
        else:
            lo, hi = int(range_from), int(range_to)
            if lo > hi:
                lo, hi = hi, lo
            if hi - lo + 1 > INVOICE_GAP_RANGE_LIMIT:
                error = f"That's {hi - lo + 1:,} numbers — narrow it to {INVOICE_GAP_RANGE_LIMIT:,} or fewer at a time."
            else:
                width = max(len(range_from), len(range_to))
                results = [_invoice_gap_check(db, rt, lo, hi, width) for rt in picked]
                total_missing = sum(r["missing_count"] for r in results)

    return templates.TemplateResponse(
        "pos/invoice_gaps.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "receipt_types": RECEIPT_TYPES, "picked": picked,
            "range_from": range_from, "range_to": range_to, "error": error,
            "results": results, "total_missing": total_missing,
        },
    )


@router.get("/pos/cancelled-receipts", response_class=HTMLResponse)
def cancelled_receipts_list(
    request: Request, receipt_type: str = "", page: int = 1,
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    page = max(page, 1)
    query = db.query(models.CancelledReceipt)
    if receipt_type:
        query = query.filter(models.CancelledReceipt.receipt_type == receipt_type)
    total = query.count()
    pages = max((total + 14) // 15, 1)
    page = min(page, pages)
    rows = (
        query.order_by(models.CancelledReceipt.cancelled_date.desc(), models.CancelledReceipt.id.desc())
        .offset((page - 1) * 15).limit(15).all()
    )
    return templates.TemplateResponse(
        "pos/cancelled_receipts.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "rows": rows, "receipt_type": receipt_type, "receipt_types": RECEIPT_TYPES,
         "page": page, "pages": pages, "total": total, "today": datetime.now(MANILA).date().isoformat()},
    )


@router.post("/pos/cancelled-receipts")
def cancelled_receipts_create(
    request: Request, receipt_type: str = Form(...), invoice_no: str = Form(...),
    cancelled_date: str = Form(""), reason: str = Form(""),
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    receipt_type = (receipt_type or "").strip().upper()
    invoice_no = (invoice_no or "").strip()
    if not receipt_type or not invoice_no:
        return RedirectResponse("/pos/cancelled-receipts?error=Receipt+type+and+invoice+%23+are+required.", status_code=302)
    try:
        c_date = date.fromisoformat(cancelled_date) if cancelled_date else datetime.now(MANILA).date()
    except ValueError:
        c_date = datetime.now(MANILA).date()

    row = models.CancelledReceipt(
        receipt_type=receipt_type, invoice_no=invoice_no, cancelled_date=c_date,
        reason=(reason or "").strip() or None, recorded_by_id=user.id,
    )
    db.add(row)
    db.flush()
    audit.record(
        db, user=user, request=request, action="create", entity_type="cancelled_receipt",
        entity_id=row.id, entity_label=f"{receipt_type} {invoice_no}",
        summary=f"Logged cancelled/unused receipt {receipt_type} #{invoice_no}" + (f" — {row.reason}" if row.reason else ""),
    )
    db.commit()
    return RedirectResponse("/pos/cancelled-receipts", status_code=302)


@router.post("/pos/cancelled-receipts/{row_id:int}/delete")
def cancelled_receipts_delete(row_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    row = db.get(models.CancelledReceipt, row_id)
    if row:
        audit.record(
            db, user=user, request=request, action="delete", entity_type="cancelled_receipt",
            entity_id=row.id, entity_label=f"{row.receipt_type} {row.invoice_no}",
            summary=f"Removed cancelled-receipt log entry {row.receipt_type} #{row.invoice_no} (data-entry correction)",
        )
        db.delete(row)
        db.commit()
    return RedirectResponse("/pos/cancelled-receipts", status_code=302)


@router.get("/pos/lookup")
def pos_lookup(invoice: str = "", receipt_type: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Find an original SALE by invoice number, for refund/exchange.

    Invoice numbers only repeat across booklets (DRS 255 and DRB 255 are
    different receipts), so a bare number can now legitimately match more
    than one sale. Returning whichever came first would refund the wrong
    receipt, so an ambiguous number comes back asking which booklet instead.
    A number typed with its booklet already picked resolves directly.
    """
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    invoice = (invoice or "").strip()
    receipt_type = (receipt_type or "").strip()
    query = db.query(models.Sale).filter(
        models.Sale.invoice_no == invoice,
        models.Sale.txn_type == "sale",
        models.Sale.is_voided.is_(False),
    )
    if receipt_type:
        query = query.filter(models.Sale.receipt_type == receipt_type)
    matches = query.all()
    if len(matches) > 1:
        return {
            "found": False,
            "ambiguous": True,
            "booklets": sorted({m.receipt_type or "(no booklet)" for m in matches}),
        }
    sale = matches[0] if matches else None
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
def pos_refund(data: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    orig = db.get(models.Sale, int(data.get("sale_id") or 0)) if data.get("sale_id") else None
    items = data.get("items") or []
    if not items:
        return JSONResponse({"ok": False, "error": "Select at least one item to refund."}, status_code=400)

    # The cashier's own invoice # (e.g. "45") wins when they typed one; only
    # auto-generate a reference (REF-45, or a sequential fallback) if they left it blank.
    typed_invoice = (data.get("invoice_no") or "").strip()
    typed_receipt_type = (data.get("receipt_type") or "").strip() or None
    if typed_invoice and _invoice_taken(db, typed_invoice, typed_receipt_type):
        return JSONResponse(
            {"ok": False, "error": f"Invoice number '{typed_invoice}' is already used"
                                   + (f" in the {typed_receipt_type} booklet." if typed_receipt_type else ".")},
            status_code=400,
        )

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
    if method not in ("cash", "gcash", "maya", "other_ewallet", "bank_transfer", "cheque"):
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
def pos_exchange(data: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    orig = db.get(models.Sale, int(data.get("sale_id") or 0)) if data.get("sale_id") else None
    returned = data.get("returned_items") or []
    new_lines = data.get("new_lines") or []
    if not returned and not new_lines:
        return JSONResponse({"ok": False, "error": "Nothing to exchange."}, status_code=400)

    typed_invoice = (data.get("invoice_no") or "").strip()
    typed_receipt_type = (data.get("receipt_type") or "").strip() or None
    if typed_invoice and _invoice_taken(db, typed_invoice, typed_receipt_type):
        return JSONResponse(
            {"ok": False, "error": f"Invoice number '{typed_invoice}' is already used"
                                   + (f" in the {typed_receipt_type} booklet." if typed_receipt_type else ".")},
            status_code=400,
        )

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
        if method not in ("cash", "gcash", "maya", "other_ewallet", "bank_transfer", "cheque", "receivable"):
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
        if method not in ("cash", "gcash", "maya", "other_ewallet", "bank_transfer", "cheque"):
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
        cheque_pdc = models.PostDatedCheque(
            direction="received", amount=_money(pending_cheque["amount"]),
            bank=pending_cheque["bank"], cheque_no=pending_cheque["cheque_no"],
            cheque_date=pending_cheque["cheque_date"],
            sale_id=ex.id, customer_id=ex.customer_id, created_by=user.id,
        )
        db.add(cheque_pdc)
        db.flush()
        db.add(models.PdcApplication(pdc_id=cheque_pdc.id, sale_id=ex.id, amount=_money(pending_cheque["amount"])))
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
    edit_payment_error: str = "",
    edit_invoice_error: str = "",
    edit_customer_error: str = "",
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

    # Existing names, for the customer-correction field's autocomplete —
    # typing a fresh name silently creates a customer, so offering what's
    # already on file is what stops "J. Santos" and "J Santos" becoming two
    # accounts. Only loaded for the staff who can actually see that field.
    customer_names = (
        [c.name for c in db.query(models.Customer.name).order_by(models.Customer.name).all()]
        if is_staff(user) else []
    )

    return templates.TemplateResponse(
        "receipt.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "sale": sale, "from": from_, "cust": cust, "quote": quote, "thermal": thermal,
         "linked": linked, "original": original, "credit_outstanding": credit_outstanding,
         "can_void": _can_void_sale(user), "void_error": VOID_ERRORS.get(void_error),
         "can_edit_date": is_staff(user), "edit_date_error": EDIT_DATE_ERRORS.get(edit_date_error),
         "can_edit_invoice": is_staff(user), "edit_invoice_error": EDIT_INVOICE_ERRORS.get(edit_invoice_error),
         "can_edit_customer": is_staff(user), "edit_customer_error": EDIT_CUSTOMER_ERRORS.get(edit_customer_error),
         "customer_names": customer_names,
         "today_iso": datetime.now(MANILA).date().isoformat(),
         "can_edit_items": is_staff(user) and _can_edit_sale_items(db, sale) is None,
         "edit_items_error": EDIT_ITEMS_ERRORS.get(edit_items_error),
         "can_edit_payment": is_staff(user) and _can_edit_sale_payment(db, sale) is None,
         "edit_payment_error": EDIT_ITEMS_ERRORS.get(edit_payment_error),
         "edit_payment_methods": EDIT_PAYMENT_METHODS, "method_labels": METHOD_LABELS,
         "is_credit_sale": (sale.receivable_amount or 0) > 0},
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

EDIT_INVOICE_ERRORS = {
    "denied": "You don't have permission to edit a sale's invoice #.",
    "voided": "This sale is voided — its invoice # can't be edited.",
    "empty": "Invoice # is required.",
    "used": "That invoice # is already used by another sale in the same booklet.",
}

EDIT_CUSTOMER_ERRORS = {
    "denied": "You don't have permission to edit a sale's customer.",
    "voided": "This sale is voided — its customer can't be edited.",
    "credit": "This sale has a credit balance, so it needs a named customer — "
              "someone has to owe it. Type the correct customer instead of clearing it.",
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
    "invalid": "Not a valid payment method to switch to.",
    "needs_customer": "Type a customer name to put this on their credit account.",
    "partially_paid": "This credit sale already has a payment recorded against it — settle or void it instead of switching methods.",
}


def _can_edit_sale_items(db: Session, sale: models.Sale):
    """Returns an EDIT_ITEMS_ERRORS key, or None if this sale is eligible for
    in-place item editing. Same underlying restrictions as void (see
    void_sale) — an edit is really "reverse the old lines, apply new ones to
    the same invoice" under the hood, so anywhere void wouldn't be safe,
    editing isn't either. A credit sale is allowed too, but only if NOTHING
    has been paid against it yet — once the customer's started paying it
    down, correcting the total here would leave a settlement referring to
    an amount that no longer matches (same "partially_paid" idea as
    switching a credit sale's payment method back to direct-tender)."""
    if sale.is_voided:
        return "voided"
    if sale.txn_type != "sale":
        return "type"
    is_credit = (sale.receivable_amount or 0) > 0
    if is_credit:
        settled = (
            db.query(func.coalesce(func.sum(models.ReceivableSettlement.amount), 0))
            .filter(models.ReceivableSettlement.sale_id == sale.id)
            .scalar()
        )
        if Decimal(str(settled or 0)) > 0:
            return "partially_paid"
    elif len(sale.payments) != 1:
        return "split"
    if db.query(models.Sale.id).filter(models.Sale.original_sale_id == sale.id).first():
        return "linked"
    if db.query(models.PostDatedCheque.id).filter(models.PostDatedCheque.sale_id == sale.id).first():
        return "pdc"
    return None


def _can_edit_sale_payment(db: Session, sale: models.Sale):
    """Same idea as _can_edit_sale_items but WITHOUT the "credit" rejection
    — a credit sale is exactly one of the two directions edit_sale_payment_
    method supports (switching it back to a direct-tender method), so this
    is the looser check the receipt page uses to decide whether to show the
    "edit payment" control at all. The route itself applies the tighter,
    direction-specific checks (e.g. "partially_paid") at submit time."""
    if sale.is_voided:
        return "voided"
    if sale.txn_type != "sale":
        return "type"
    if db.query(models.Sale.id).filter(models.Sale.original_sale_id == sale.id).first():
        return "linked"
    if db.query(models.PostDatedCheque.id).filter(models.PostDatedCheque.sale_id == sale.id).first():
        return "pdc"
    is_credit = (sale.receivable_amount or 0) > 0
    if not is_credit and len(sale.payments) != 1:
        return "split"
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
            ref=_display_invoice(sale), unit_cost=unit_cost, value=base_qty * unit_cost,
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


@router.post("/pos/receipt/{sale_id:int}/edit-invoice")
def edit_sale_invoice(
    sale_id: int,
    request: Request,
    new_invoice_no: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Correct the invoice # on an already-saved sale — for a typo when
    copying the number off the physical booklet. Staff only, and the new
    number must not collide with another sale's."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    sale = db.get(models.Sale, sale_id)
    if not sale:
        return RedirectResponse("/pos", status_code=302)

    def _back(err=None):
        suffix = f"&edit_invoice_error={err}" if err else ""
        return RedirectResponse(f"/pos/receipt/{sale_id}?from=sales{suffix}", status_code=302)

    if not is_staff(user):
        return _back("denied")
    if sale.is_voided:
        return _back("voided")

    new_invoice_no = (new_invoice_no or "").strip()
    if not new_invoice_no:
        return _back("empty")
    if new_invoice_no == sale.invoice_no:
        return _back()
    if _invoice_taken(db, new_invoice_no, sale.receipt_type, exclude_sale_id=sale.id):
        return _back("used")

    old_invoice_no = sale.invoice_no
    sale.invoice_no = new_invoice_no
    audit.record(
        db, user=user, request=request, action="update", entity_type="sale",
        entity_id=sale.id, entity_label=sale.invoice_no,
        summary=f"Corrected invoice # for sale: {old_invoice_no} → {new_invoice_no}",
        changes={"invoice_no": [old_invoice_no, new_invoice_no]},
    )
    db.commit()
    return _back()


@router.post("/pos/receipt/{sale_id:int}/edit-customer")
def edit_sale_customer(
    sale_id: int,
    request: Request,
    new_customer_name: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Correct who a sale was rung up to — a misspelled name, or the wrong
    customer picked entirely. Staff only.

    On a credit sale this genuinely moves the debt: the customer's statement
    is built from the sales pointing at them, so re-pointing this one moves
    the invoice AND every payment already collected on it (settlements hang
    off the sale, not the customer) onto the corrected account. That's the
    right behaviour for "I picked the wrong customer" — but it's also why
    clearing the name back to Walk-in is refused while credit is outstanding:
    a receivable with nobody attached is money owed by no one.
    """
    if not user:
        return RedirectResponse("/login", status_code=302)
    sale = db.get(models.Sale, sale_id)
    if not sale:
        return RedirectResponse("/pos", status_code=302)

    def _back(err=None):
        suffix = f"&edit_customer_error={err}" if err else ""
        return RedirectResponse(f"/pos/receipt/{sale_id}?from=sales{suffix}", status_code=302)

    if not is_staff(user):
        return _back("denied")
    if sale.is_voided:
        return _back("voided")

    new_customer_name = (new_customer_name or "").strip()
    old_customer_name = sale.customer_name or "Walk-in"
    if new_customer_name == (sale.customer_name or ""):
        return _back()

    if not new_customer_name:
        if (sale.receivable_amount or Decimal("0")) > 0:
            return _back("credit")
        sale.customer_id = None
        sale.customer_name = None
    else:
        customer = get_or_create_customer(db, new_customer_name)
        sale.customer_id = customer.id if customer else None
        sale.customer_name = new_customer_name

    audit.record(
        db, user=user, request=request, action="update", entity_type="sale",
        entity_id=sale.id, entity_label=sale.invoice_no,
        summary=f"Corrected customer on {sale.invoice_no}: {old_customer_name} → {new_customer_name or 'Walk-in'}",
        changes={"customer_name": [old_customer_name, new_customer_name or "Walk-in"]},
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


@router.post("/pos/receipt/{sale_id:int}/edit-payment")
def edit_sale_payment_method(
    sale_id: int, request: Request,
    new_method: str = Form(""),
    customer_name: str = Form(""),
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    """Correct the payment method on an already-finalized sale — e.g. the
    cashier rang it up as Cash by mistake when the customer actually paid
    GCash, or a walk-in cash sale actually needs to go on the customer's
    credit account instead. Two directions, each with its own eligibility:

    Direct-tender <-> direct-tender (e.g. Cash -> GCash): same guard as
    item editing (_can_edit_sale_items) — a single-payment, non-credit,
    non-voided, non-linked plain sale, since this is really "reverse the
    old payment, apply the new one" under the hood.

    Direct-tender -> Credit: same guard, PLUS the sale needs a customer
    (typed in if it was a Walk-in sale) so it has somewhere to become an
    outstanding balance. Sets receivable_amount/due_date exactly like a
    credit sale rung up that way from the start.

    Credit -> direct-tender: only if NOTHING has been paid against it yet
    (zero ReceivableSettlement) — once the customer's started paying it
    down, this needs Credits' own tools, not this shortcut. Everything
    else item editing checks (voided/type/linked/PDC) still applies.

    Every path re-posts the ledger: reverse what was posted, then post
    fresh with the corrected method, so the money moves to the right
    account instead of leaving a stale posting behind."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    sale = db.get(models.Sale, sale_id)
    if not sale:
        return RedirectResponse("/pos", status_code=302)

    def _back(err=None):
        suffix = f"&edit_payment_error={err}" if err else ""
        return RedirectResponse(f"/pos/receipt/{sale_id}?from=sales{suffix}", status_code=302)

    if not is_staff(user):
        return _back("denied")

    new_method = (new_method or "").strip().lower()
    is_currently_credit = (sale.receivable_amount or 0) > 0

    if is_currently_credit and new_method != "receivable":
        # Credit -> direct-tender. Same base checks as item editing, minus
        # the "credit" one (that's the whole point here) — done by hand
        # instead of calling _can_edit_sale_items, which would reject a
        # credit sale outright.
        if sale.is_voided:
            return _back("voided")
        if sale.txn_type != "sale":
            return _back("type")
        if db.query(models.Sale.id).filter(models.Sale.original_sale_id == sale.id).first():
            return _back("linked")
        if db.query(models.PostDatedCheque.id).filter(models.PostDatedCheque.sale_id == sale.id).first():
            return _back("pdc")
        if new_method not in EDIT_PAYMENT_METHODS:
            return _back("invalid")
        settled = (
            db.query(func.coalesce(func.sum(models.ReceivableSettlement.amount), 0))
            .filter(models.ReceivableSettlement.sale_id == sale.id)
            .scalar()
        )
        if Decimal(str(settled or 0)) > 0:
            return _back("partially_paid")

        sale.receivable_amount = Decimal("0")
        sale.due_date = None
        sale.amount_tendered = sale.total
        sale.change_amount = Decimal("0")
        sale.payment_method = METHOD_LABELS[new_method]
        db.add(models.Payment(sale_id=sale.id, method=new_method, amount=sale.total))

        accounting.reverse_sale_posting(db, sale, reason=f"Payment method corrected: receivable -> {new_method}", entered_by_id=user.id)
        try:
            accounting.post_sale(db, sale, method_rows=[(new_method, sale.total)], receivable_amount=Decimal("0"), entered_by_id=user.id)
        except accounting.PostingError:
            pass
        audit.record(
            db, user=user, request=request, action="update", entity_type="sale",
            entity_id=sale.id, entity_label=sale.invoice_no,
            summary=f"Corrected payment method for sale {sale.invoice_no}: receivable → {new_method}",
            changes={"payment_method": ["receivable", new_method]},
        )
        db.commit()
        return _back()

    if not is_currently_credit and new_method == "receivable":
        # Direct-tender -> Credit.
        block_reason = _can_edit_sale_items(db, sale)
        if block_reason:
            return _back(block_reason)
        customer_id = sale.customer_id
        if not customer_id:
            typed_name = (customer_name or "").strip()
            if not typed_name:
                return _back("needs_customer")
            customer = get_or_create_customer(db, typed_name)
            customer_id = customer.id
            sale.customer_id = customer_id
            sale.customer_name = customer.name
        customer = db.get(models.Customer, customer_id)
        days = customer.credit_days if customer and customer.credit_days is not None else 15

        old_method = sale.payments[0].method
        db.query(models.Payment).filter(models.Payment.sale_id == sale.id).delete(synchronize_session=False)
        sale.receivable_amount = sale.total
        sale.amount_tendered = Decimal("0")
        sale.change_amount = Decimal("0")
        sale.due_date = date.today() + timedelta(days=int(days))
        sale.payment_method = METHOD_LABELS["receivable"]

        accounting.reverse_sale_posting(db, sale, reason=f"Payment method corrected: {old_method} -> receivable", entered_by_id=user.id)
        try:
            accounting.post_sale(db, sale, method_rows=[], receivable_amount=sale.total, entered_by_id=user.id)
        except accounting.PostingError:
            pass
        audit.record(
            db, user=user, request=request, action="update", entity_type="sale",
            entity_id=sale.id, entity_label=sale.invoice_no,
            summary=f"Corrected payment method for sale {sale.invoice_no}: {old_method} → receivable (due {sale.due_date})",
            changes={"payment_method": [old_method, "receivable"]},
        )
        db.commit()
        return _back()

    # Direct-tender -> direct-tender (unchanged from before).
    block_reason = _can_edit_sale_items(db, sale)
    if block_reason:
        return _back(block_reason)
    if new_method not in EDIT_PAYMENT_METHODS:
        return _back("invalid")

    payment = sale.payments[0]
    old_method = payment.method
    if new_method == old_method:
        return _back()

    payment.method = new_method
    sale.payment_method = METHOD_LABELS[new_method]

    accounting.reverse_sale_posting(db, sale, reason=f"Payment method corrected: {old_method} -> {new_method}", entered_by_id=user.id)
    try:
        accounting.post_sale(
            db, sale, method_rows=[(new_method, sale.total)], receivable_amount=Decimal("0"), entered_by_id=user.id,
        )
    except accounting.PostingError:
        pass

    audit.record(
        db, user=user, request=request, action="update", entity_type="sale",
        entity_id=sale.id, entity_label=sale.invoice_no,
        summary=f"Corrected payment method for sale {sale.invoice_no}: {old_method} → {new_method}",
        changes={"payment_method": [old_method, new_method]},
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
def edit_sale_items(sale_id: int, data: dict, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
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
            ref=_display_invoice(sale), unit_cost=unit_cost, value=base_qty * unit_cost,
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
            product_id=product.id, qty_base=-base_qty, reason="sale", ref=_display_invoice(sale),
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

    # _can_edit_sale_items guarantees either exactly one non-credit payment,
    # or an unsettled credit sale — auto-adjust whichever one applies to
    # match the corrected total exactly (per shop's own choice: this is a
    # same-day correction, not a new sale).
    if (sale.receivable_amount or 0) > 0:
        sale.receivable_amount = _money(total)
        # _finalize_sale records a Payment row even for a receivable/cheque
        # "method" (see its `for method, amount in method_rows` loop) — keep
        # it in sync too, or it's left showing the pre-edit amount forever.
        for payment in sale.payments:
            if payment.method in ("receivable", "cheque"):
                payment.amount = _money(total)
    else:
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

    display_invoice = f"{sale.receipt_type}{sale.invoice_no}" if (sale.receipt_type and sale.invoice_no) else sale.invoice_no
    doc_meta = [
        f"Invoice #: {display_invoice}",
        f"Date: {sale.created_at.strftime('%b %d, %Y %I:%M %p') if sale.created_at else ''}",
    ]
    if sale.cashier:
        doc_meta.append(f"Cashier: {sale.cashier.full_name or sale.cashier.username}")

    party_lines = [sale.customer_name or "Walk-in"]
    if sale.customer and sale.customer.tin:
        party_lines.append(f"TIN {sale.customer.tin}")
    # The sale's own delivery address (editable per sale) wins over the
    # customer's saved one — same as what's shown on the on-screen receipt.
    delivery_addr = sale.delivery_address or (sale.customer.address if sale.customer else None)
    if delivery_addr:
        party_lines.append(f"Delivery Address: {delivery_addr}")
    if sale.notes:
        party_lines.append(f"Note: {sale.notes}")

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
