"""Inventory / Products module: encode products with beginning stock.

Columns the client asked for: Product Name, Category, Unit Type, Cost of Sales,
Selling Price, Actual Beginning Stocks, Stocks Qty, Total Qty.
"""
import base64
import csv
import io
import json
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import barcode as barcode_lib
from barcode.writer import SVGWriter
import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from fastapi import APIRouter, Depends, File, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import audit, models, pricing, settings_store
from .database import get_db
from .deps import get_current_user, is_admin
from .templating import templates

router = APIRouter()

PAGE_SIZE = 20
CENTS = Decimal("0.01")

BULK_MODES = ("pct", "amount", "markup")

# Fields whose before/after we log on a product edit. Stock and price changes
# are the theft/accountability-sensitive ones the owner most wants visible.
AUDIT_FIELDS = ["name", "barcode", "cost_price", "selling_price", "beginning_stock", "stock_qty",
                "reorder_level", "is_vat", "markup_pct", "markup_price", "margin_pct", "margin_price"]

# Why a manual stock quantity changed — required whenever an edit moves
# Beginning Stock or Stock Qty, so the eventual peso value (qty * cost) has
# an accountable reason attached, not just a silent number change.
ADJUSTMENT_REASONS = [
    ("count_correction", "Count correction"),
    ("damage", "Damage / breakage"),
    ("theft", "Theft / loss"),
    ("expired", "Expired / spoiled"),
    ("initial_balance", "Initial balance correction"),
    ("other", "Other"),
]
ADJUSTMENT_REASON_LABELS = dict(ADJUSTMENT_REASONS)


def _product_snapshot(p: models.Product) -> dict:
    d = audit.snapshot(p, AUDIT_FIELDS)
    d["category"] = p.category.name if p.category else None
    d["unit_type"] = p.unit_type.name if p.unit_type else None
    return d

# Columns used for the import template and the upload parser.
TEMPLATE_HEADERS = [
    "Product Name",
    "Category",
    "Unit Type",
    "Cost of Sales",
    "Selling Price",
    "Actual Beginning Stocks",
    "Stocks Qty",
]

# Maps (normalized) spreadsheet headers -> internal field names, so columns can
# be in any order and tolerate small naming differences.
HEADER_MAP = {
    "product name": "name", "name": "name", "product": "name",
    "category": "category",
    "unit type": "unit_type", "unit": "unit_type", "unit of measure": "unit_type", "uom": "unit_type",
    "cost of sales": "cost", "cost": "cost", "cost price": "cost",
    "selling price": "selling", "price": "selling", "srp": "selling",
    "actual beginning stocks": "beginning", "beginning stock": "beginning",
    "beginning stocks": "beginning", "beginning": "beginning",
    "stocks qty": "stocks", "stock qty": "stocks", "stocks": "stocks", "stock": "stocks",
    "vat": "vat", "vat-able": "vat", "vatable": "vat",
}
FIELDS = ["name", "category", "unit_type", "cost", "selling", "beginning", "stocks", "vat"]


def _to_decimal(value: str, default: str = "0") -> Decimal:
    value = (value or "").strip().replace(",", "")
    if value == "":
        value = default
    try:
        return Decimal(value)
    except InvalidOperation:
        return Decimal(default)


def _get_or_create_category(db: Session, name: str):
    name = (name or "").strip()
    if not name:
        return None
    existing = db.query(models.Category).filter(func.lower(models.Category.name) == name.lower()).first()
    if existing:
        return existing
    cat = models.Category(name=name)
    db.add(cat)
    db.flush()
    return cat


def _get_or_create_unit_type(db: Session, name: str):
    name = (name or "").strip()
    if not name:
        return None
    existing = db.query(models.UnitType).filter(func.lower(models.UnitType.name) == name.lower()).first()
    if existing:
        return existing
    unit = models.UnitType(name=name)
    db.add(unit)
    db.flush()
    return unit


_needs_review_expr = pricing.needs_review_expr


@router.get("/products", response_class=HTMLResponse)
def list_products(
    request: Request,
    q: str = "",
    page: int = 1,
    alert: int = 0,
    category_id: int = 0,
    bulk_msg: str = "",
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)

    q = (q or "").strip()
    page = max(page, 1)

    query = db.query(models.Product).filter(models.Product.is_active.is_(True))
    if q:
        query = query.filter(
            models.Product.name.ilike(f"%{q}%") | (models.Product.barcode == q)
        )
    if alert:
        # Only products whose selling price no longer covers cost.
        query = query.filter(
            models.Product.cost_price > 0,
            models.Product.selling_price <= models.Product.cost_price,
        )
    if category_id:
        query = query.filter(models.Product.category_id == category_id)

    total = query.count()
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, pages)

    qty_expr = models.Product.beginning_stock + models.Product.stock_qty
    total_cost_value, total_retail_value = query.with_entities(
        func.coalesce(func.sum(qty_expr * models.Product.cost_price), 0),
        func.coalesce(func.sum(qty_expr * models.Product.selling_price), 0),
    ).one()
    total_cost_value = Decimal(str(total_cost_value or 0))
    total_retail_value = Decimal(str(total_retail_value or 0))

    products = (
        query.order_by(models.Product.name)
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
        .all()
    )

    # Quick-filter pills: only categories actually in use, with a live count
    # each, computed against the current search/alert filter (not the
    # category filter itself) so switching pills reflects what's really there.
    base_for_counts = db.query(models.Product).filter(models.Product.is_active.is_(True))
    if q:
        base_for_counts = base_for_counts.filter(
            models.Product.name.ilike(f"%{q}%") | (models.Product.barcode == q)
        )
    if alert:
        base_for_counts = base_for_counts.filter(
            models.Product.cost_price > 0,
            models.Product.selling_price <= models.Product.cost_price,
        )
    cat_counts = dict(
        base_for_counts.filter(models.Product.category_id.isnot(None))
        .with_entities(models.Product.category_id, func.count(models.Product.id))
        .group_by(models.Product.category_id)
        .all()
    )
    categories = (
        db.query(models.Category)
        .filter(models.Category.id.in_(cat_counts.keys()))
        .order_by(models.Category.name)
        .all()
    ) if cat_counts else []

    return templates.TemplateResponse(
        "products/list.html",
        {
            "request": request,
            "app_name": request.app.title,
            "user": user,
            "products": products,
            "q": q,
            "page": page,
            "pages": pages,
            "total": total,
            "total_cost_value": total_cost_value,
            "total_retail_value": total_retail_value,
            "alert": alert,
            "bulk_msg": bulk_msg,
            "category_id": category_id,
            "categories": categories,
            "cat_counts": cat_counts,
        },
    )


def _render_form(request, db, user, product=None, error=None):
    categories = db.query(models.Category).order_by(models.Category.name).all()
    unit_types = db.query(models.UnitType).order_by(models.UnitType.name).all()
    return templates.TemplateResponse(
        "products/form.html",
        {
            "request": request,
            "app_name": request.app.title,
            "user": user,
            "product": product,
            "categories": categories,
            "unit_types": unit_types,
            "adjustment_reasons": ADJUSTMENT_REASONS,
            "error": error,
        },
    )


@router.get("/products/new", response_class=HTMLResponse)
def new_product(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    return _render_form(request, db, product=None, user=user)


@router.get("/products/{product_id:int}/edit", response_class=HTMLResponse)
def edit_product(product_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    product = db.get(models.Product, product_id)
    if not product:
        return RedirectResponse("/products", status_code=302)
    return _render_form(request, db, user, product=product)


def _save_from_form(product: models.Product, db: Session, form):
    product.name = (form.get("name") or "").strip()
    product.barcode = (form.get("barcode") or "").strip() or None
    product.category = _get_or_create_category(db, form.get("category"))
    product.unit_type = _get_or_create_unit_type(db, form.get("unit_type"))
    product.cost_price = _to_decimal(form.get("cost_price"))
    # Selling prices are set on the Add-product form and from then on only
    # through the dedicated Selling Price tab (see /products/pricing) — the
    # edit form here doesn't carry these fields, so leave the fixed price and
    # both percentages untouched. Markup/margin prices still get refreshed
    # against whatever the cost is now, so they don't go stale if it changed.
    markup_pct = form.get("markup_pct") if "markup_pct" in form else product.markup_pct
    margin_pct = form.get("margin_pct") if "margin_pct" in form else product.margin_pct
    if "selling_price" in form:
        product.selling_price = _to_decimal(form.get("selling_price"))   # the fixed price
    pricing.apply_to(product, product.cost_price, markup_pct, margin_pct)
    product.beginning_stock = _to_decimal(form.get("beginning_stock"))
    product.stock_qty = _to_decimal(form.get("stock_qty"))
    product.reorder_level = _to_decimal(form.get("reorder_level"))
    product.is_vat = bool(form.get("is_vat"))

    # Units ladder (extra sellable units). Parallel arrays from the form.
    names = form.getlist("unit_name")
    factors = form.getlist("unit_factor")
    prices = form.getlist("unit_price")
    product.units.clear()
    order = 0
    for i, nm in enumerate(names):
        nm = (nm or "").strip()
        if not nm:
            continue
        fac = _to_decimal(factors[i] if i < len(factors) else "1", "1")
        if fac <= 0:
            fac = Decimal("1")
        pr = _to_decimal(prices[i] if i < len(prices) else "0")
        product.units.append(
            models.ProductUnit(name=nm, factor_to_base=fac, price=pr, sort_order=order)
        )
        order += 1


@router.post("/products")
async def create_product(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    if not (form.get("name") or "").strip():
        return _render_form(request, db, user, product=None, error="Product name is required.")
    barcode = (form.get("barcode") or "").strip()
    if barcode and db.query(models.Product).filter(models.Product.barcode == barcode).first():
        return _render_form(request, db, user, product=None, error=f"Barcode “{barcode}” is already assigned to another product.")
    product = models.Product()
    _save_from_form(product, db, form)
    db.add(product)
    db.flush()  # assign product.id so the audit row can reference it
    audit.record(
        db, user=user, request=request, action="create", entity_type="product",
        entity_id=product.id, entity_label=product.name,
        summary=f"Created product “{product.name}”",
    )
    db.commit()
    return RedirectResponse("/products", status_code=status.HTTP_302_FOUND)


@router.post("/products/{product_id:int}")
async def update_product(product_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    product = db.get(models.Product, product_id)
    if not product:
        return RedirectResponse("/products", status_code=302)
    form = await request.form()
    if not (form.get("name") or "").strip():
        return _render_form(request, db, user, product=product, error="Product name is required.")
    barcode = (form.get("barcode") or "").strip()
    if barcode and db.query(models.Product).filter(models.Product.barcode == barcode, models.Product.id != product.id).first():
        return _render_form(request, db, user, product=product, error=f"Barcode “{barcode}” is already assigned to another product.")
    old_total = Decimal(str(product.total_qty or 0))
    new_total_preview = _to_decimal(form.get("beginning_stock")) + _to_decimal(form.get("stock_qty"))
    adjustment_reason = (form.get("adjustment_reason") or "").strip()
    if new_total_preview != old_total and not adjustment_reason:
        return _render_form(request, db, user, product=product,
                             error="Select a reason for the stock quantity change before saving.")

    before = _product_snapshot(product)
    _save_from_form(product, db, form)
    db.flush()
    after = _product_snapshot(product)
    new_total = Decimal(str(product.total_qty or 0))
    changes = audit.diff(before, after)
    if changes:
        # Flag a stock correction distinctly — it's the theft-sensitive edit.
        stock_touched = "stock_qty" in changes or "beginning_stock" in changes
        audit.record(
            db, user=user, request=request,
            action="adjust_stock" if stock_touched else "update",
            entity_type="product", entity_id=product.id, entity_label=product.name,
            summary=(f"Adjusted stock for “{product.name}”" if stock_touched else f"Edited “{product.name}”"),
            changes=changes,
        )
        # Record the net stock change as a movement too, so the Stock Card
        # ledger reconciles — manual edits used to leave no trace here. Value
        # it at current cost so it also shows up as shrinkage/gain in P&L.
        delta = new_total - old_total
        if delta != 0:
            unit_cost = Decimal(str(product.cost_price or 0))
            reason_label = ADJUSTMENT_REASON_LABELS.get(adjustment_reason, adjustment_reason or "manual edit")
            note_text = (form.get("adjustment_note") or "").strip()
            db.add(models.StockMovement(
                product_id=product.id, qty_base=delta, reason="adjustment", ref="manual edit",
                unit_cost=unit_cost, value=delta * unit_cost,
                note=f"{reason_label}: {note_text}" if note_text else reason_label,
            ))
    db.commit()
    return RedirectResponse("/products", status_code=status.HTTP_302_FOUND)


@router.get("/products/pricing", response_class=HTMLResponse)
def pricing_tool(request: Request, review: int = 0, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """The single unified costing + pricing workflow: search or browse on
    the left (with a 'Price Review Needed' filter folded in as a toggle,
    not a separate tab), edit cost + the 3-row pricing matrix on the right."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    min_margin = settings_store.min_margin_pct()
    review_count = db.query(func.count(models.Product.id)).filter(
        models.Product.is_active.is_(True), _needs_review_expr(min_margin)
    ).scalar() or 0
    return templates.TemplateResponse(
        "products/pricing.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "review_count": review_count,
            "initial_review": bool(review),
        },
    )


@router.get("/products/price-search")
def price_search(
    q: str = "",
    review: int = 0,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    q = (q or "").strip()
    min_margin = settings_store.min_margin_pct()

    review_count = db.query(func.count(models.Product.id)).filter(
        models.Product.is_active.is_(True), _needs_review_expr(min_margin)
    ).scalar() or 0

    query = db.query(models.Product).filter(models.Product.is_active.is_(True))
    if review:
        query = query.filter(_needs_review_expr(min_margin))
    if q:
        barcode_hit = query.filter(models.Product.barcode == q).first()
        products = [barcode_hit] if barcode_hit else (
            query.filter(models.Product.name.ilike(f"%{q}%")).order_by(models.Product.name).limit(100).all()
        )
    else:
        products = query.order_by(models.Product.name).limit(200 if review else 60).all()

    rows = []
    for p in products:
        flagged, reason = pricing.needs_review(p.selling_price, p.cost_price, min_margin)
        rows.append({
            "id": p.id, "name": p.name, "barcode": p.barcode,
            "cost_price": float(p.cost_price or 0),
            "selling_price": float(p.selling_price or 0),
            "markup_pct": float(p.markup_pct or 0),
            "margin_pct": float(p.margin_pct or 0),
            "needs_review": flagged, "review_reason": reason,
        })
    return {"products": rows, "review_count": review_count}


@router.post("/products/{product_id:int}/pricing")
async def update_pricing(product_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    product = db.get(models.Product, product_id)
    if not product:
        return JSONResponse({"ok": False, "error": "Product not found."}, status_code=404)
    data = await request.json()
    before = _product_snapshot(product)
    if "cost_price" in data:
        product.cost_price = _to_decimal(data.get("cost_price"))
    product.selling_price = _to_decimal(data.get("selling_price"))
    pricing.apply_to(product, product.cost_price, data.get("markup_pct"), data.get("margin_pct"))
    db.flush()
    after = _product_snapshot(product)
    changes = audit.diff(before, after)
    if changes:
        audit.record(
            db, user=user, request=request, action="update",
            entity_type="product", entity_id=product.id, entity_label=product.name,
            summary=f"Updated selling price for “{product.name}”",
            changes=changes,
        )
    db.commit()
    min_margin = settings_store.min_margin_pct()
    flagged, reason = pricing.needs_review(product.selling_price, product.cost_price, min_margin)
    return {
        "ok": True,
        "cost_price": float(product.cost_price or 0),
        "selling_price": float(product.selling_price or 0),
        "markup_pct": float(product.markup_pct or 0),
        "margin_pct": float(product.margin_pct or 0),
        "needs_review": flagged, "review_reason": reason,
    }


@router.post("/products/{product_id:int}/archive")
def archive_product(product_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    product = db.get(models.Product, product_id)
    if product:
        product.is_active = False
        audit.record(
            db, user=user, request=request, action="archive", entity_type="product",
            entity_id=product.id, entity_label=product.name,
            summary=f"Archived “{product.name}”",
        )
        db.commit()
    return RedirectResponse("/products", status_code=status.HTTP_302_FOUND)


def _bulk_mode_label(mode: str, value: Decimal) -> str:
    sign = "+" if value >= 0 else ""
    if mode == "pct":
        return f"Bulk price update: {sign}{value:g}% on Fixed Price"
    if mode == "amount":
        return f"Bulk price update: {sign}{value:g} on Fixed Price"
    return f"Bulk price update: Markup set to {value:g}%"


@router.post("/products/bulk-price", response_class=HTMLResponse)
async def bulk_price_start(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Step 1: show the selected products with a live client-side preview.
    Nothing is saved here — Apply (below) recomputes and saves for real."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/products", status_code=302)
    form = await request.form()
    ids = sorted({int(i) for i in form.getlist("ids") if i.isdigit()})
    if not ids:
        return RedirectResponse("/products", status_code=302)
    products = (
        db.query(models.Product)
        .filter(models.Product.id.in_(ids), models.Product.is_active.is_(True))
        .order_by(models.Product.name)
        .all()
    )
    return templates.TemplateResponse(
        "products/bulk_price.html",
        {"request": request, "app_name": request.app.title, "user": user, "products": products},
    )


@router.post("/products/bulk-price/apply")
async def bulk_price_apply(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Step 2: the server recomputes every price from scratch (never trusts
    numbers the browser sent) and saves, logging one audit row per product
    that actually changed — so this shows up on that product's normal price
    history exactly like a one-off edit would."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/products", status_code=302)
    form = await request.form()
    ids = sorted({int(i) for i in form.getlist("ids") if i.isdigit()})
    mode = (form.get("mode") or "").strip()
    if mode not in BULK_MODES:
        mode = "pct"
    try:
        value = Decimal(str(form.get("value") or "0").strip().replace(",", ""))
    except InvalidOperation:
        value = Decimal("0")

    if not ids:
        return RedirectResponse("/products", status_code=302)

    products = (
        db.query(models.Product)
        .filter(models.Product.id.in_(ids), models.Product.is_active.is_(True))
        .all()
    )
    label = _bulk_mode_label(mode, value)
    updated = 0
    skipped = 0
    for p in products:
        if mode == "markup" and (not p.cost_price or p.cost_price <= 0):
            skipped += 1
            continue
        before = _product_snapshot(p)
        if mode == "pct":
            new_price = (Decimal(str(p.selling_price or 0)) * (1 + value / 100)).quantize(CENTS, rounding=ROUND_HALF_UP)
            p.selling_price = max(new_price, Decimal("0"))
        elif mode == "amount":
            new_price = (Decimal(str(p.selling_price or 0)) + value).quantize(CENTS, rounding=ROUND_HALF_UP)
            p.selling_price = max(new_price, Decimal("0"))
        else:  # markup
            p.markup_pct = value
            p.markup_price = pricing.markup_price(p.cost_price, value)
        after = _product_snapshot(p)
        changes = audit.diff(before, after)
        if changes:
            audit.record(
                db, user=user, request=request, action="update", entity_type="product",
                entity_id=p.id, entity_label=p.name, summary=label, changes=changes,
            )
            updated += 1
    db.commit()
    msg = f"Updated {updated} product{'s' if updated != 1 else ''}"
    if skipped:
        msg += f", skipped {skipped} with no cost on file"
    return RedirectResponse(f"/products?bulk_msg={msg}", status_code=status.HTTP_302_FOUND)


def _generate_internal_barcode(product_id: int) -> str:
    """For products with no manufacturer barcode (locally-sourced, repackaged
    goods) — a locally-unique code we invent and print ourselves. No need to
    mimic a real EAN-13/UPC checksum scheme; Code128 (used to render it)
    reads any digit/letter string fine."""
    return f"LC{product_id:08d}"


def _barcode_data_uri(code: str) -> str:
    """Render `code` as a Code128 barcode (works for any barcode we already
    have on file — manufacturer EAN-13 digits included, Code128 just encodes
    them as text) and return it as an inline data: URI, ready for an <img>."""
    buf = io.BytesIO()
    bc = barcode_lib.get("code128", code, writer=SVGWriter())
    bc.write(buf, options={"write_text": False, "quiet_zone": 2})
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


@router.post("/products/labels", response_class=HTMLResponse)
async def print_labels(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/products", status_code=302)
    form = await request.form()
    ids = sorted({int(i) for i in form.getlist("ids") if i.isdigit()})
    if not ids:
        return RedirectResponse("/products", status_code=302)
    products = (
        db.query(models.Product)
        .filter(models.Product.id.in_(ids), models.Product.is_active.is_(True))
        .order_by(models.Product.name)
        .all()
    )
    assigned = False
    for p in products:
        if not p.barcode:
            p.barcode = _generate_internal_barcode(p.id)
            assigned = True
    if assigned:
        db.commit()

    labels = [{"product": p, "barcode_uri": _barcode_data_uri(p.barcode)} for p in products]
    return templates.TemplateResponse(
        "products/labels.html",
        {"request": request, "app_name": request.app.title, "user": user, "labels": labels},
    )


# Human labels + in/out direction for the stock-movement reasons written across
# POS (sale/refund/exchange), Purchasing (purchase/return) and manual edits.
MOVEMENT_LABELS = {
    "sale": "Sale", "refund": "Refund (returned)",
    "exchange-return": "Exchange — returned in", "exchange-sale": "Exchange — sold out",
    "purchase": "Purchase received", "purchase-return": "Purchase return",
    "adjustment": "Manual adjustment",
}


@router.get("/products/{product_id:int}/stock-card", response_class=HTMLResponse)
def stock_card(product_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Per-product stock ledger: every in/out movement with a running balance.

    The balance is anchored to the product's *current* on-hand total and worked
    backwards through the movements, so the newest row always shows the true
    current stock even if older movements pre-date movement tracking (e.g. a
    bulk import). The implied opening balance is shown for reconciliation.
    """
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    product = db.get(models.Product, product_id)
    if not product:
        return RedirectResponse("/products", status_code=302)

    movements = (
        db.query(models.StockMovement)
        .filter(models.StockMovement.product_id == product_id)
        .order_by(models.StockMovement.created_at.asc(), models.StockMovement.id.asc())
        .all()
    )

    current_total = Decimal(str(product.total_qty or 0))
    total_delta = sum((Decimal(str(m.qty_base or 0)) for m in movements), Decimal("0"))
    opening = current_total - total_delta

    running = opening
    total_in = total_out = Decimal("0")
    rows = []
    for m in movements:
        delta = Decimal(str(m.qty_base or 0))
        running += delta
        if delta >= 0:
            total_in += delta
        else:
            total_out += -delta
        rows.append({
            "movement": m,
            "label": MOVEMENT_LABELS.get(m.reason, (m.reason or "").replace("-", " ").title()),
            "in_qty": delta if delta > 0 else None,
            "out_qty": -delta if delta < 0 else None,
            "balance": running,
        })
    rows.reverse()  # newest first for display

    return templates.TemplateResponse(
        "products/stock_card.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "product": product, "rows": rows, "opening": opening,
            "current_total": current_total, "total_in": total_in, "total_out": total_out,
            "count": len(movements),
        },
    )


@router.get("/products/{product_id:int}/history")
def product_history(product_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Cost and selling-price over time, for the Dashboard's per-item tracking
    chart. Built from two real sources, not a dedicated price-log table:

      cost  — PurchaseLine.new_cost at each confirmed delivery (the actual
              moment a product's cost changes), plus any manual cost_price
              edit found in the Activity Log (a cost can also be corrected by
              hand, not only by receiving stock).
      price — the Activity Log's before/after on selling_price, since a price
              only ever changes through an admin edit — there's no separate
              event source for it the way purchases are for cost.

    Both series end with today's live value as the last point, so the line
    always reaches "now" even if the last recorded change was a while ago.
    The Activity Log only goes back to when it was built, so a product's
    history here effectively starts from then for anything not tied to a
    purchase.
    """
    if not user:
        return {"found": False}
    if not is_admin(user):
        return {"found": False}
    product = db.get(models.Product, product_id)
    if not product or not product.is_active:
        return {"found": False}

    today = date.today()
    now = datetime.now()
    # points[key] = {date, ts, value, label, old}. Keyed by a unique event id
    # (not by date) so multiple changes on the same day each keep their own
    # point instead of the later one overwriting the earlier one. `ts` (full
    # timestamp) drives ordering and the chart's x-position so same-day points
    # don't collapse onto each other; `date` is just the display label.
    # `old` (when known — a purchase line's old_cost, or an audit diff's
    # before-value) is what carried this series before this specific point,
    # so a single recorded change can still be seeded with an earlier
    # baseline instead of showing as a lone dot.
    cost_points = {}
    price_points = {}

    # ---- cost: confirmed purchases -----------------------------------------
    purchase_rows = (
        db.query(models.PurchaseLine, models.Purchase)
        .join(models.Purchase, models.PurchaseLine.purchase_id == models.Purchase.id)
        .filter(
            models.PurchaseLine.product_id == product_id,
            models.Purchase.txn_type == "receive",
            models.Purchase.status.in_(("confirmed", "paid")),
            models.Purchase.confirmed_at.isnot(None),
        )
        .order_by(models.Purchase.confirmed_at)
        .all()
    )
    for line, purchase in purchase_rows:
        cost_points[f"purchase-{line.id}"] = {
            "date": purchase.confirmed_at.date().isoformat(), "ts": purchase.confirmed_at.isoformat(),
            "value": float(line.new_cost or 0), "label": f"Received via {purchase.ref_no}",
            "old": float(line.old_cost or 0),
        }

    # ---- cost + price: manual edits, from the Activity Log ----------------
    audit_rows = (
        db.query(models.AuditLog)
        .filter(models.AuditLog.entity_type == "product", models.AuditLog.entity_id == product_id,
                models.AuditLog.action.in_(("create", "update")))
        .order_by(models.AuditLog.created_at)
        .all()
    )
    for row in audit_rows:
        if not row.changes:
            continue
        try:
            changes = json.loads(row.changes)
        except ValueError:
            continue
        d = row.created_at.date().isoformat()
        ts = row.created_at.isoformat()
        if "cost_price" in changes:
            try:
                old, new = changes["cost_price"]
                cost_points[f"audit-{row.id}-cost"] = {"date": d, "ts": ts, "value": float(new), "label": "Manual cost edit",
                                                         "old": float(old) if old not in (None, "") else None}
            except (TypeError, ValueError):
                pass
        if "selling_price" in changes:
            try:
                old, new = changes["selling_price"]
                price_points[f"audit-{row.id}-price"] = {"date": d, "ts": ts, "value": float(new), "label": "Manual price edit",
                                                           "old": float(old) if old not in (None, "") else None}
            except (TypeError, ValueError):
                pass

    def _finalize(points: dict, current_value: float, current_label: str) -> list:
        series = sorted(points.values(), key=lambda p: p["ts"])
        # A single change is still real movement — seed a baseline at the
        # product's creation date using that change's "before" value, so it
        # draws as a slope ("was X since added, changed to Y on this date")
        # instead of one disconnected dot. Only when that's actually earlier
        # than anything already in the series.
        if series and series[0].get("old") is not None and product.created_at:
            if product.created_at < datetime.fromisoformat(series[0]["ts"]):
                series.insert(0, {
                    "date": product.created_at.date().isoformat(), "ts": product.created_at.isoformat(),
                    "value": series[0]["old"], "label": "Since product was added",
                })
        today_iso = today.isoformat()
        if not series or series[-1]["date"] != today_iso:
            series.append({"date": today_iso, "ts": now.isoformat(), "value": current_value, "label": current_label})
        return [{"date": p["date"], "ts": p["ts"], "value": p["value"], "label": p["label"]} for p in series]

    cost_series = _finalize(cost_points, float(product.cost_price or 0), "Current cost")
    price_series = _finalize(price_points, float(product.selling_price or 0), "Current price")

    return {
        "found": True,
        "product": {"id": product.id, "name": product.name, "cost_price": float(product.cost_price or 0),
                     "selling_price": float(product.selling_price or 0)},
        "cost_series": cost_series,
        "price_series": price_series,
    }


# --------------------------------------------------------------------------- #
# Bulk import (Excel / CSV)
# --------------------------------------------------------------------------- #
def _parse_bool(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "y", "yes", "true", "vat", "x", "✓", "oui"}


def _parse_upload(filename: str, contents: bytes):
    """Return (rows, error). rows is a list of dicts keyed by FIELDS."""
    name = (filename or "").lower()
    try:
        if name.endswith(".csv"):
            text = contents.decode("utf-8-sig", errors="replace")
            table = list(csv.reader(io.StringIO(text)))
        elif name.endswith(".xlsx") or name.endswith(".xlsm"):
            wb = openpyxl.load_workbook(io.BytesIO(contents), read_only=True, data_only=True)
            ws = wb.active
            table = [list(row) for row in ws.iter_rows(values_only=True)]
        else:
            return None, "Unsupported file type. Please upload a .xlsx or .csv file."
    except Exception as exc:  # noqa: BLE001
        return None, f"Could not read the file: {exc}"

    if not table:
        return None, "The file appears to be empty."

    header = table[0]
    idx = {}
    for i, cell in enumerate(header):
        key = str(cell or "").strip().lower()
        if key in HEADER_MAP:
            idx[HEADER_MAP[key]] = i
    if "name" not in idx:
        return None, (
            "Missing a 'Product Name' column. Download the template to see the "
            "expected format."
        )

    def cell(raw, field):
        i = idx.get(field)
        if i is None or i >= len(raw):
            return ""
        val = raw[i]
        return "" if val is None else str(val).strip()

    rows = []
    for raw in table[1:]:
        if not raw:
            continue
        record = {f: cell(raw, f) for f in FIELDS}
        if not any(record[f] for f in FIELDS):  # skip blank rows
            continue
        rows.append(record)
    return rows, None


@router.get("/products/import", response_class=HTMLResponse)
def import_form(request: Request, user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(
        "products/import.html",
        {"request": request, "app_name": request.app.title, "user": user, "result": None},
    )


@router.post("/products/import", response_class=HTMLResponse)
async def import_upload(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)

    contents = await file.read()
    rows, error = _parse_upload(file.filename, contents)

    if error:
        result = {"error": error}
    else:
        created = updated = skipped = 0
        errors = []
        for line_no, record in enumerate(rows, start=2):  # row 2 = first data row
            name = record["name"].strip()
            if not name:
                skipped += 1
                continue
            try:
                existing = (
                    db.query(models.Product)
                    .filter(func.lower(models.Product.name) == name.lower())
                    .filter(models.Product.is_active.is_(True))
                    .first()
                )
                product = existing or models.Product()
                product.name = name
                product.category = _get_or_create_category(db, record["category"])
                product.unit_type = _get_or_create_unit_type(db, record["unit_type"])
                product.cost_price = _to_decimal(record["cost"])
                product.selling_price = _to_decimal(record["selling"])
                product.beginning_stock = _to_decimal(record["beginning"])
                product.stock_qty = _to_decimal(record["stocks"])
                product.is_vat = _parse_bool(record["vat"])
                if existing:
                    updated += 1
                else:
                    db.add(product)
                    created += 1
            except Exception as exc:  # noqa: BLE001
                errors.append({"row": line_no, "name": name, "message": str(exc)})
        if created or updated:
            audit.record(
                db, user=user, request=request, action="update", entity_type="product",
                entity_label=file.filename,
                summary=f"Bulk import from “{file.filename}”: {created} created, {updated} updated, {skipped} skipped",
            )
        db.commit()
        result = {
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "errors": errors,
            "total": len(rows),
            "filename": file.filename,
        }

    return templates.TemplateResponse(
        "products/import.html",
        {"request": request, "app_name": request.app.title, "user": user, "result": result},
    )


@router.get("/products/import/template")
def download_template(user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Products"
    ws.append(TEMPLATE_HEADERS)

    header_fill = PatternFill("solid", fgColor="1F6FEB")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill

    # Example rows (delete these before importing your real data).
    # The second row deliberately leaves "Cost of Sales" blank: cost is optional
    # and gets filled in automatically when you receive stock in Purchasing.
    ws.append(["Portland Cement 40kg", "Cement", "Bag", 220, 260, 10, 5])
    ws.append(["Common Wire Nail #4", "Fasteners", "Kg", None, 95, 25.5, 0])

    widths = [26, 16, 12, 14, 14, 24, 12]
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="product_import_template.xlsx"'},
    )
