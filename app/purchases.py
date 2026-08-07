"""Purchasing: receive goods from a supplier, or return goods to them.

A receive-type purchase has a status lifecycle, same idea as Quotations:
  pending   -> a Purchase Order raised with a supplier; nothing in stock yet.
  confirmed -> the delivery physically arrived: stock is added and each
               product's cost price is updated to what was actually paid.
  paid      -> payment was later settled with the supplier; no stock effect.
A return has no staging — it removes stock immediately, same as before.
"""
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request, status as http_status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from . import audit, models, pricing, settings_store
from .database import get_db
from .deps import get_current_user, is_staff
from .pos import _resolve_txn_datetime
from .products import _get_or_create_category, _get_or_create_unit_type
from .templating import templates

router = APIRouter()

CENTS = Decimal("0.01")
COST_DP = Decimal("0.0001")


def _weighted_avg_cost(product: models.Product, base_qty: Decimal, unit_cost_per_base: Decimal) -> Decimal:
    """Blend an incoming receipt into the product's cost, weighted by quantity
    on hand right now. This IS the product's cost going forward — there's no
    separate 'last cost' field, so every markup/margin price and the min-margin
    warning move with the true blended cost, not just the latest invoice."""
    old_qty = Decimal(str(product.total_qty or 0))
    if old_qty <= 0 or base_qty <= 0:
        return unit_cost_per_base.quantize(CENTS, rounding=ROUND_HALF_UP)
    old_cost = Decimal(str(product.cost_price or 0))
    blended = ((old_qty * old_cost) + (base_qty * unit_cost_per_base)) / (old_qty + base_qty)
    return blended.quantize(CENTS, rounding=ROUND_HALF_UP)

PAYMENT_METHODS = [("cash", "Cash"), ("bank_transfer", "Bank Transfer"), ("cheque", "Cheque"), ("gcash", "GCash"), ("other", "Other")]
STATUS_LABELS = {"paid": "Paid", "cancelled": "Cancelled"}
PAGE_SIZE = 15


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
    except (InvalidOperation, AttributeError, ValueError):
        return Decimal(default)


def _money(value) -> Decimal:
    return _dec(value).quantize(CENTS, rounding=ROUND_HALF_UP)


def _margin_check(cost: Decimal, price: Decimal):
    cost = cost or Decimal("0")
    price = price or Decimal("0")
    if cost <= 0:
        return None
    if price <= 0:
        return {"level": "danger", "message": "No selling price set."}
    if price < cost:
        return {"level": "danger", "message": f"Selling below cost by {(cost - price):.2f}"}
    if price == cost:
        return {"level": "warn", "message": "Selling price equals cost — zero margin."}
    return None


def margin_alert(product: models.Product):
    """Alert when a product's CURRENT (live) selling price no longer clears cost."""
    if not product:
        return None
    return _margin_check(product.cost_price, product.selling_price)


def _purchase_product_payload(p: models.Product) -> dict:
    """Shape a product for the purchase form's picker (units by name+factor
    only — a purchase line's cost is typed in, not chosen from a price)."""
    base_unit = p.unit_type.name if p.unit_type else "Unit"
    units = [{"name": base_unit, "factor": 1.0}]
    for u in p.units:
        units.append({"name": u.name, "factor": float(u.factor_to_base or 1)})
    return {
        "id": p.id,
        "name": p.name,
        "base_unit": base_unit,
        "has_unit_type": p.unit_type_id is not None,
        "units": units,
        "cost_price": float(p.cost_price or 0),
        "selling_price": float(p.selling_price or 0),
        "on_hand": float(p.total_qty or 0),
    }


@router.get("/purchases/search")
def purchase_search(q: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Product lookup for the purchase form (includes current cost/selling price)."""
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not is_staff(user):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    q = (q or "").strip()
    query = db.query(models.Product).filter(models.Product.is_active.is_(True))
    if q:
        query = query.filter(models.Product.name.ilike(f"%{q}%"))
    products = query.order_by(models.Product.name).limit(30).all()
    return {"products": [_purchase_product_payload(p) for p in products]}


@router.get("/purchases/product/{product_id:int}")
def purchase_product(product_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """A single product's current units/cost — lets the pending-PO editor offer
    unit switching on a line that predates opening the editor."""
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not is_staff(user):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    p = db.get(models.Product, product_id)
    if not p or not p.is_active:
        return {"found": False}
    return {"found": True, "product": _purchase_product_payload(p)}


@router.get("/purchases", response_class=HTMLResponse)
def list_purchases(
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
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)

    page = max(page, 1)
    q = (q or "").strip()
    df, dt = _parse_date(date_from), _parse_date(date_to)

    query = db.query(models.Purchase)
    if status_filter == "return":
        query = query.filter(models.Purchase.txn_type == "return")
    elif status_filter in STATUS_LABELS:
        query = query.filter(models.Purchase.txn_type == "receive", models.Purchase.status == status_filter)
    if q:
        like = f"%{q}%"
        query = query.outerjoin(models.Supplier, models.Purchase.supplier_id == models.Supplier.id).filter(
            or_(models.Purchase.ref_no.ilike(like), models.Purchase.invoice_no.ilike(like), models.Supplier.name.ilike(like))
        )
    if df:
        query = query.filter(_local_date(models.Purchase.created_at) >= df)
    if dt:
        query = query.filter(_local_date(models.Purchase.created_at) <= dt)
    total = query.count()
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, pages)
    purchases = (
        query.order_by(models.Purchase.id.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
        .all()
    )

    all_purchases = db.query(models.Purchase).all()
    received = sum((p.total or Decimal("0")) for p in all_purchases if p.txn_type != "return")
    returned = sum((p.total or Decimal("0")) for p in all_purchases if p.txn_type == "return")
    counts = {s: sum(1 for p in all_purchases if p.txn_type == "receive" and p.status == s) for s in STATUS_LABELS}
    counts["return"] = sum(1 for p in all_purchases if p.txn_type == "return")

    return templates.TemplateResponse(
        "purchases/list.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "purchases": purchases, "received": received, "returned": returned,
         "status_filter": status_filter, "counts": counts, "labels": STATUS_LABELS,
         "q": q, "date_from": date_from, "date_to": date_to,
         "page": page, "pages": pages, "total": total},
    )


DUE_SOON_DAYS = 15   # same window as Receivables (sales.py) and Notifications


def _settled_for_purchases(db: Session, purchase_ids):
    """Total paid so far per purchase — the AP mirror of sales.py's
    _settled_map, sourced from PurchaseSettlement instead of
    ReceivableSettlement."""
    if not purchase_ids:
        return {}
    rows = (
        db.query(models.PurchaseSettlement.purchase_id, func.coalesce(func.sum(models.PurchaseSettlement.amount), 0))
        .filter(models.PurchaseSettlement.purchase_id.in_(purchase_ids))
        .group_by(models.PurchaseSettlement.purchase_id)
        .all()
    )
    return {pid: Decimal(amt) for pid, amt in rows}


def _purchase_outstanding(purchase, settled_map) -> Decimal:
    return (purchase.total or Decimal("0")) - settled_map.get(purchase.id, Decimal("0"))


@router.get("/purchases/payables", response_class=HTMLResponse)
def list_payables(
    request: Request,
    q: str = "",
    supplier_id: int = 0,
    page: int = 1,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """What we owe suppliers: every confirmed receiving with a balance still
    outstanding after any partial payments, with aging.

    A 'confirmed' receive-type purchase IS the payable — goods and cost
    already moved, payment hasn't (fully). Mirrors Sales -> Receivables:
    PurchaseSettlement tracks partial payments the same way
    ReceivableSettlement does, so a payable only drops off this list once
    it's actually paid down to zero, not necessarily in one shot.
    """
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)

    q = (q or "").strip()
    page = max(page, 1)
    today = date.today()
    horizon = today + timedelta(days=DUE_SOON_DAYS)

    settled_sub = (
        db.query(
            models.PurchaseSettlement.purchase_id.label("pid"),
            func.coalesce(func.sum(models.PurchaseSettlement.amount), 0).label("paid"),
        )
        .group_by(models.PurchaseSettlement.purchase_id)
        .subquery()
    )
    outstanding_expr = models.Purchase.total - func.coalesce(settled_sub.c.paid, 0)
    query = (
        db.query(models.Purchase, outstanding_expr.label("outstanding"))
        .outerjoin(settled_sub, settled_sub.c.pid == models.Purchase.id)
        .filter(models.Purchase.txn_type == "receive", models.Purchase.status == "confirmed")
        .filter(outstanding_expr > 0)
    )
    if supplier_id:
        query = query.filter(models.Purchase.supplier_id == supplier_id)
    if q:
        like = f"%{q}%"
        query = query.outerjoin(models.Supplier, models.Purchase.supplier_id == models.Supplier.id).filter(
            or_(models.Purchase.ref_no.ilike(like), models.Purchase.invoice_no.ilike(like), models.Supplier.name.ilike(like))
        )

    total_count, total_owed = query.with_entities(
        func.count(models.Purchase.id), func.coalesce(func.sum(outstanding_expr), 0)
    ).one()
    pages = max((total_count + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, pages)
    page_rows = (
        query.order_by(func.coalesce(models.Purchase.due_date, models.Purchase.confirmed_at).asc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
        .all()
    )
    rows = [(p, Decimal(str(out))) for p, out in page_rows]

    overdue_count = sum(1 for p, _ in rows if p.due_date and p.due_date < today)
    due_soon_count = sum(1 for p, _ in rows if p.due_date and today <= p.due_date <= horizon)

    suppliers = db.query(models.Supplier).order_by(models.Supplier.name).all()

    return templates.TemplateResponse(
        "purchases/payables.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "rows": rows, "today": today, "horizon": horizon,
            "total_count": total_count, "total_owed": Decimal(str(total_owed or 0)),
            "overdue_count": overdue_count, "due_soon_count": due_soon_count,
            "q": q, "supplier_id": supplier_id, "suppliers": suppliers,
            "page": page, "pages": pages,
        },
    )


@router.get("/purchases/payables/aging", response_class=HTMLResponse)
def payables_aging(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """AP aging schedule: every outstanding payable bucketed by how overdue
    it is, grouped by supplier — the report Payables itself doesn't give you
    (that's just a flat due-date-ordered list)."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)

    today = date.today()
    purchases = (
        db.query(models.Purchase)
        .filter(models.Purchase.txn_type == "receive", models.Purchase.status == "confirmed")
        .order_by(models.Purchase.supplier_id, models.Purchase.due_date)
        .all()
    )
    settled = _settled_for_purchases(db, [p.id for p in purchases])

    BUCKETS = ["current", "1_30", "31_60", "61_90", "over_90"]
    BUCKET_LABELS = {
        "current": "Current", "1_30": "1–30 days", "31_60": "31–60 days",
        "61_90": "61–90 days", "over_90": "Over 90 days",
    }

    def bucket_for(due_date):
        if not due_date or due_date >= today:
            return "current"
        days = (today - due_date).days
        if days <= 30:
            return "1_30"
        if days <= 60:
            return "31_60"
        if days <= 90:
            return "61_90"
        return "over_90"

    by_supplier = {}
    grand_totals = {b: Decimal("0") for b in BUCKETS}
    grand_total = Decimal("0")
    for p in purchases:
        outstanding = _purchase_outstanding(p, settled)
        if outstanding <= 0:
            continue
        b = bucket_for(p.due_date)
        name = p.supplier.name if p.supplier else "—"
        row = by_supplier.setdefault(name, {b2: Decimal("0") for b2 in BUCKETS})
        row[b] += outstanding
        grand_totals[b] += outstanding
        grand_total += outstanding

    supplier_rows = sorted(
        ({"supplier": name, **totals, "total": sum(totals.values(), Decimal("0"))} for name, totals in by_supplier.items()),
        key=lambda r: r["total"], reverse=True,
    )

    return templates.TemplateResponse(
        "purchases/aging.html",
        {
            "request": request, "app_name": request.app.title, "user": user,
            "buckets": BUCKETS, "bucket_labels": BUCKET_LABELS,
            "supplier_rows": supplier_rows, "grand_totals": grand_totals, "grand_total": grand_total,
            "today": today,
        },
    )


@router.get("/purchases/new", response_class=HTMLResponse)
def new_purchase(
    request: Request, supplier: int = 0, return_against: int = 0,
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)

    # Returns are started FROM a specific received delivery (its "Return to
    # supplier" button), not typed freehand — that way whoever's returning
    # something has already looked at what was actually received before
    # deciding to send it back.
    return_against_po = None
    return_lines_prefill = []
    if return_against:
        candidate = db.get(models.Purchase, return_against)
        if candidate and candidate.txn_type == "receive" and candidate.status == "paid":
            return_against_po = candidate
            supplier = candidate.supplier_id or supplier
            # Pre-fill the cart with exactly what was received on this
            # delivery — the cashier checks it against what's on the shelf
            # and adjusts/removes lines for whatever's actually going back,
            # instead of re-searching the whole catalog from scratch.
            for pl in candidate.lines:
                if not pl.product_id:
                    continue
                product = db.get(models.Product, pl.product_id)
                if not product:
                    continue
                payload = _product_payload(product)
                unit_index = next(
                    (i for i, u in enumerate(payload["units"]) if u["name"] == pl.unit_name), 0
                )
                return_lines_prefill.append({
                    "product": payload,
                    "unitIndex": unit_index,
                    "qty": float(pl.qty or 0),
                    "unitCost": float(pl.unit_cost or 0),
                })

    suppliers = (
        db.query(models.Supplier)
        .filter(models.Supplier.is_active.is_(True))
        .order_by(models.Supplier.name)
        .all()
    )
    categories = db.query(models.Category).order_by(models.Category.name).all()
    unit_types = db.query(models.UnitType).order_by(models.UnitType.name).all()
    return templates.TemplateResponse(
        "purchases/form.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "suppliers": suppliers, "preselect": supplier,
         "return_against_po": return_against_po, "return_lines_prefill": return_lines_prefill,
         "categories": categories, "unit_types": unit_types, "payment_methods": PAYMENT_METHODS},
    )


def _product_payload(p: models.Product) -> dict:
    """Shape a product the way the purchase form expects it."""
    base_unit = p.unit_type.name if p.unit_type else "Unit"
    units = [{"name": base_unit, "factor": 1.0}]
    for u in p.units:
        units.append({"name": u.name, "factor": float(u.factor_to_base or 1)})
    return {
        "id": p.id,
        "name": p.name,
        "base_unit": base_unit,
        "units": units,
        "cost_price": float(p.cost_price or 0),
        "selling_price": float(p.selling_price or 0),
        "on_hand": float(p.total_qty or 0),
    }


@router.post("/purchases/quick-product")
async def quick_product(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Create a product that isn't in inventory yet, straight from the purchase form."""
    if not user:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if not is_staff(user):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

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
        # Already there — just hand it back so the cashier can carry on.
        return {"ok": True, "existed": True, "product": _product_payload(existing)}

    # Cost may be typed here so the markup/margin prices can be worked out up
    # front; it also pre-fills this purchase line. Confirming the purchase still
    # sets the authoritative cost from what's actually received.
    cost = _money(data.get("cost_price") or 0)
    product = models.Product(
        name=name,
        cost_price=cost,
        selling_price=_money(data.get("selling_price") or 0),
        beginning_stock=Decimal("0"),
        stock_qty=Decimal("0"),
        is_active=True,
    )
    pricing.apply_to(product, cost, data.get("markup_pct"), data.get("margin_pct"))
    product.category = _get_or_create_category(db, data.get("category"))
    product.unit_type = _get_or_create_unit_type(db, data.get("unit_type") or "Piece")
    db.add(product)
    db.commit()
    db.refresh(product)
    return {"ok": True, "existed": False, "product": _product_payload(product)}




@router.post("/purchases")
async def create_purchase(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if not is_staff(user):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    data = await request.json()
    lines = data.get("lines") or []
    if not lines:
        return JSONResponse({"ok": False, "error": "Add at least one item."}, status_code=400)

    txn_type = "return" if (data.get("txn_type") == "return") else "receive"
    supplier_id = data.get("supplier_id")
    supplier_id = int(supplier_id) if supplier_id else None
    if not supplier_id:
        return JSONResponse({"ok": False, "error": "Choose a supplier."}, status_code=400)

    # Optional backdating for a purchase entered after the fact — lands it on
    # the day it happened in every date-based report; blank keeps live 'now'.
    backdated, date_err = _resolve_txn_datetime(data.get("txn_date"))
    if date_err:
        return JSONResponse({"ok": False, "error": date_err}, status_code=400)
    stamp = backdated if backdated else func.now()

    # For a return: optionally link back to the delivery it's coming from.
    original_purchase_id = None
    if txn_type == "return":
        raw_orig = data.get("original_purchase_id")
        if raw_orig:
            original = db.get(models.Purchase, int(raw_orig))
            if not original or original.txn_type != "receive" or original.supplier_id != supplier_id:
                return JSONResponse(
                    {"ok": False, "error": "That delivery doesn't match this supplier."}, status_code=400
                )
            original_purchase_id = original.id

    # Stocked and costed in one step either way — that part never waits.
    # Payment is usually settled the same time (Cash/Bank Transfer/Cheque/
    # GCash/Other), but "Payable (Unpaid)" leaves it owed to the supplier
    # instead, same as the old confirmed-but-unpaid stage — it just shows up
    # under Payables now rather than being a separate step to get there.
    payment_method = None
    is_payable = False
    if txn_type == "receive":
        raw_method = (data.get("payment_method") or "cash").strip().lower()
        if raw_method == "payable":
            is_payable = True
        else:
            payment_method = raw_method if raw_method in dict(PAYMENT_METHODS) else "cash"

    supplier = db.get(models.Supplier, supplier_id)
    due_date = None
    if is_payable:
        days = supplier.payment_days if supplier and supplier.payment_days is not None else 30
        base_date = backdated.date() if backdated else date.today()
        due_date = base_date + timedelta(days=int(days))

    purchase = models.Purchase(
        txn_type=txn_type,
        status="confirmed" if (txn_type == "return" or is_payable) else "paid",
        created_at=stamp,
        confirmed_at=stamp,
        paid_at=stamp if (txn_type == "receive" and not is_payable) else None,
        payment_method=payment_method,
        due_date=due_date,
        supplier_id=supplier_id,
        original_purchase_id=original_purchase_id,
        invoice_no=(data.get("invoice_no") or "").strip() or None,
        delivery_date=(data.get("delivery_date") or "").strip() or None,
        notes=(data.get("notes") or "").strip() or None,
        user_id=user.id,
    )
    db.add(purchase)

    total = Decimal("0")
    for ln in lines:
        # Row-level lock — see the matching comment in pos.py's _finalize_sale.
        product = db.get(models.Product, int(ln["product_id"]), with_for_update=True) if ln.get("product_id") else None
        if not product and txn_type == "receive":
            # Product Name typed in the purchase row didn't match anything —
            # create it right here from what was typed, so receiving doesn't
            # stall on a separate "add product" step. Re-check by name first
            # (case-insensitive) in case it exists but the row lost its match
            # (e.g. the name was re-typed after a pick).
            name = (ln.get("product_name") or "").strip()
            if not name:
                continue
            product = (
                db.query(models.Product)
                .filter(func.lower(models.Product.name) == name.lower(), models.Product.is_active.is_(True))
                .first()
            )
            if not product:
                product = models.Product(
                    name=name,
                    cost_price=Decimal("0"), selling_price=Decimal("0"),
                    beginning_stock=Decimal("0"), stock_qty=Decimal("0"),
                    unit_type=_get_or_create_unit_type(db, (ln.get("unit_name") or "Piece").strip() or "Piece"),
                    is_active=True,
                )
                db.add(product)
                db.flush()  # assign product.id for the StockMovement/PurchaseLine below
                audit.record(
                    db, user=user, request=request, action="create", entity_type="product",
                    entity_id=product.id, entity_label=product.name,
                    summary=f"Created product “{product.name}” from purchase receiving",
                )
        if not product:
            continue
        if txn_type == "receive" and not product.unit_type_id:
            # Inventory had no Unit Type on file for this product (that's why
            # the row's Unit field was left editable instead of locked) —
            # whatever was typed there becomes its Unit Type going forward.
            typed_unit = (ln.get("unit_name") or "").strip()
            if typed_unit:
                product.unit_type = _get_or_create_unit_type(db, typed_unit)
        qty = _dec(ln.get("qty"))
        if qty <= 0:
            continue
        factor = _dec(ln.get("factor"), "1")
        if factor <= 0:
            factor = Decimal("1")
        unit_cost = _dec(ln.get("unit_cost"))
        # The cashier may type the line Total directly (it back-computes the unit
        # cost on screen). Trust that figure so the printed total matches exactly.
        raw_total = ln.get("line_total")
        line_total = _money(raw_total) if raw_total not in (None, "") else _money(qty * unit_cost)
        total += line_total

        base_qty = qty * factor
        old_cost = Decimal(str(product.cost_price or 0))
        new_cost = old_cost

        if txn_type == "return":
            product.stock_qty = (product.stock_qty or Decimal("0")) - base_qty
            db.add(models.StockMovement(
                product_id=product.id, qty_base=-base_qty, reason="purchase-return",
                unit_cost=old_cost, value=-base_qty * old_cost,
            ))
        else:
            if unit_cost > 0:
                new_cost = _weighted_avg_cost(product, base_qty, unit_cost / factor)
                product.cost_price = new_cost
            product.stock_qty = (product.stock_qty or Decimal("0")) + base_qty
            db.add(models.StockMovement(
                product_id=product.id, qty_base=base_qty, reason="purchase",
                unit_cost=new_cost, value=base_qty * new_cost,
            ))

        purchase.lines.append(models.PurchaseLine(
            product_id=product.id,
            product_name=product.name,
            unit_name=ln.get("unit_name"),
            unit_factor=factor,
            qty=qty,
            unit_cost=_money(unit_cost),
            line_total=line_total,
            old_cost=old_cost.quantize(COST_DP),
            new_cost=new_cost.quantize(COST_DP),
        ))

    if not purchase.lines:
        return JSONResponse({"ok": False, "error": "No valid items to save."}, status_code=400)

    purchase.total = _money(total)
    db.flush()
    prefix = "PRET" if txn_type == "return" else "PO"
    # A receive keyed to the supplier's own invoice/DR # is easier to match
    # against their paperwork than an internal sequence number — use it as
    # the ref # when given, as long as it doesn't collide with another PO.
    ref_no = None
    if txn_type == "receive" and purchase.invoice_no:
        candidate = f"PO-{purchase.invoice_no}"[:30]
        taken = db.query(models.Purchase).filter(models.Purchase.ref_no == candidate).first()
        if not taken:
            ref_no = candidate
    purchase.ref_no = ref_no or f"{prefix}-{purchase.id:06d}"

    if txn_type == "receive" and payment_method == "cheque":
        raw_date = (data.get("cheque_date") or "").strip()
        try:
            cheque_date = date.fromisoformat(raw_date)
        except ValueError:
            cheque_date = date.today()
        pdc = models.PostDatedCheque(
            direction="issued", amount=purchase.total,
            bank=(data.get("bank") or "").strip() or None,
            cheque_no=(data.get("cheque_no") or "").strip() or None,
            cheque_date=cheque_date,
            purchase_id=purchase.id, supplier_id=supplier_id,
            created_by=user.id,
        )
        db.add(pdc)

    db.commit()
    return {"ok": True, "purchase_id": purchase.id, "ref_no": purchase.ref_no}


@router.post("/purchases/{purchase_id:int}/pay")
async def settle_purchase_pay(purchase_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Settle a payable — in full or in part. Stock and cost already moved
    when the purchase was created; this only closes out what's owed, same
    partial-payment idea as a customer's credit (see sales.settle_pay)."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    purchase = db.get(models.Purchase, purchase_id)
    if not purchase or purchase.txn_type != "receive" or purchase.status != "confirmed":
        return RedirectResponse(f"/purchases/{purchase_id}", status_code=http_status.HTTP_302_FOUND)

    settled = _settled_for_purchases(db, [purchase.id])
    outstanding = _purchase_outstanding(purchase, settled)
    form = await request.form()
    method = (form.get("method") or "cash").strip().lower()
    if method not in dict(PAYMENT_METHODS):
        method = "cash"
    amount = _dec(form.get("amount"))

    def back_with_error(error):
        return RedirectResponse(
            f"/purchases/{purchase_id}?error={error}", status_code=http_status.HTTP_302_FOUND
        )

    if amount <= 0:
        return back_with_error("Enter+an+amount+greater+than+zero.")
    if amount > outstanding:
        amount = outstanding  # never pay more than owed

    if method == "cheque":
        # A post-dated cheque doesn't settle anything yet — same as the AR
        # side, it just goes into the PDC register until the bank honors it.
        raw_date = (form.get("cheque_date") or "").strip()
        try:
            cheque_date = date.fromisoformat(raw_date)
        except ValueError:
            return back_with_error("Enter+a+valid+cheque+date+%28the+date+printed+on+the+cheque%29.")
        pdc = models.PostDatedCheque(
            direction="issued", amount=_money(amount),
            bank=(form.get("bank") or "").strip() or None,
            cheque_no=(form.get("cheque_no") or "").strip() or None,
            cheque_date=cheque_date,
            purchase_id=purchase.id, supplier_id=purchase.supplier_id,
            created_by=user.id,
        )
        db.add(pdc)
        db.flush()
        db.commit()
        return RedirectResponse(f"/pdc/{pdc.id}", status_code=http_status.HTTP_302_FOUND)

    db.add(models.PurchaseSettlement(
        purchase_id=purchase.id, method=method, amount=_money(amount), created_by=user.id,
    ))
    new_outstanding = outstanding - amount
    summary = f"Recorded a {dict(PAYMENT_METHODS)[method]} payment of {amount:.2f} on {purchase.ref_no}"
    if new_outstanding <= 0:
        purchase.status = "paid"
        purchase.payment_method = method
        purchase.paid_at = func.now()
        summary = f"Paid {purchase.ref_no} in full via {dict(PAYMENT_METHODS)[method]}"
    audit.record(
        db, user=user, request=request, action="update", entity_type="purchase",
        entity_id=purchase.id, entity_label=purchase.ref_no, summary=summary,
    )
    db.commit()
    return RedirectResponse(f"/purchases/{purchase_id}", status_code=http_status.HTTP_302_FOUND)


@router.post("/purchases/{purchase_id:int}/cancel")
def cancel_purchase(purchase_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Void a logged purchase. For a receive, this reverses the stock it
    added (a matching negative movement) — the weighted-average cost blend
    itself isn't unwound (later purchases may have already blended on top of
    it), which is a known, accepted simplification. A return has nothing to
    reverse beyond its own stock effect, same idea."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    purchase = db.get(models.Purchase, purchase_id)
    if not purchase or purchase.status == "cancelled":
        return RedirectResponse(f"/purchases/{purchase_id}", status_code=http_status.HTTP_302_FOUND)

    for line in purchase.lines:
        if not line.product_id:
            continue
        product = db.get(models.Product, line.product_id, with_for_update=True)
        if not product:
            continue
        base_qty = (line.qty or Decimal("0")) * (line.unit_factor or Decimal("1"))
        delta = -base_qty if purchase.txn_type == "receive" else base_qty
        product.stock_qty = (product.stock_qty or Decimal("0")) + delta
        cancel_unit_cost = Decimal(str(line.new_cost or product.cost_price or 0))
        db.add(models.StockMovement(
            product_id=product.id, qty_base=delta, reason="purchase-cancelled",
            unit_cost=cancel_unit_cost, value=delta * cancel_unit_cost,
        ))

    purchase.status = "cancelled"
    purchase.cancelled_at = func.now()
    audit.record(
        db, user=user, request=request, action="cancel", entity_type="purchase",
        entity_id=purchase.id, entity_label=purchase.ref_no,
        summary=f"Cancelled {purchase.ref_no} — stock reversed",
    )
    db.commit()
    return RedirectResponse(f"/purchases/{purchase_id}", status_code=http_status.HTTP_302_FOUND)


@router.get("/purchases/{purchase_id:int}", response_class=HTMLResponse)
def view_purchase(
    purchase_id: int,
    request: Request,
    error: str = "",
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    purchase = db.get(models.Purchase, purchase_id)
    if not purchase:
        return RedirectResponse("/purchases", status_code=302)

    # Which lines changed the cost, and which products now need a price review?
    rows = []
    for ln in purchase.lines:
        old = Decimal(str(ln.old_cost or 0))
        new = Decimal(str(ln.new_cost or 0))
        alert = margin_alert(ln.product)
        rows.append({
            "line": ln,
            "changed": purchase.txn_type != "return" and new != old,
            "increased": new > old,
            "diff": new - old,
            "alert": alert,
        })
    alerts = [r for r in rows if r["alert"]]

    # Cross-links: if this IS a return, the delivery it came from; if this
    # IS a delivery, any returns made from it (there can be more than one).
    linked_returns = (
        db.query(models.Purchase)
        .filter(models.Purchase.original_purchase_id == purchase.id)
        .order_by(models.Purchase.id)
        .all()
    )

    # An issued cheque already pending for this purchase — don't let another get issued too.
    pending_pdc = (
        db.query(models.PostDatedCheque)
        .filter(
            models.PostDatedCheque.purchase_id == purchase.id,
            models.PostDatedCheque.status == "pending",
        )
        .first()
    )

    settlements = (
        db.query(models.PurchaseSettlement)
        .filter(models.PurchaseSettlement.purchase_id == purchase.id)
        .order_by(models.PurchaseSettlement.created_at)
        .all()
    )
    settled_map = _settled_for_purchases(db, [purchase.id])
    outstanding = _purchase_outstanding(purchase, settled_map)

    return templates.TemplateResponse(
        "purchases/view.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "purchase": purchase, "rows": rows, "alerts": alerts,
         "linked_returns": linked_returns, "error": error, "pending_pdc": pending_pdc,
         "settlements": settlements, "outstanding": outstanding,
         "today": date.today(), "payment_methods": PAYMENT_METHODS,
         "payment_method_labels": dict(PAYMENT_METHODS)},
    )


@router.get("/purchases/{purchase_id:int}/pdf")
def purchase_pdf(purchase_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """A simple downloadable PDF of the Purchase Order — supplier, the items
    received/returned, and the total. Matches the on-screen PO view."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    purchase = db.get(models.Purchase, purchase_id)
    if not purchase:
        return RedirectResponse("/purchases", status_code=302)

    import io
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from .pdf_utils import letterhead

    biz = settings_store.get_all(db)
    doc_label = "Purchase Return" if purchase.txn_type == "return" else "Purchase Order"

    doc_meta = [
        f"Ref #: {purchase.ref_no}",
        f"Date: {purchase.created_at.strftime('%b %d, %Y %I:%M %p') if purchase.created_at else ''}",
    ]
    if purchase.invoice_no:
        doc_meta.append(f"Supplier Invoice: {purchase.invoice_no}")

    party_lines = []
    if purchase.supplier:
        s = purchase.supplier
        party_lines.append(s.name)
        if s.contact_person:
            party_lines.append(s.contact_person)
        if s.mobile:
            party_lines.append(s.mobile)
        if s.address:
            party_lines.append(s.address)
        if s.tin:
            party_lines.append(f"TIN {s.tin}")
    else:
        party_lines.append("-")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=18 * mm, bottomMargin=18 * mm, leftMargin=18 * mm, rightMargin=18 * mm)
    styles = getSampleStyleSheet()
    elements = letterhead(biz, doc_label, doc_meta, "Supplier", party_lines)

    table_data = [["Item", "Unit", "Qty", "Unit Cost", "Total"]]
    for ln in purchase.lines:
        table_data.append([
            ln.product_name,
            ln.unit_name or "",
            f"{float(ln.qty):g}",
            f"{ln.unit_cost:,.2f}",
            f"{ln.line_total:,.2f}",
        ])
    table_data.append(["", "", "", "Total", f"{purchase.total:,.2f}"])

    table = Table(table_data, colWidths=[170, 80, 60, 90, 90], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F6FEB")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE", (0, -1), (-1, -1), 1, colors.black),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 14))
    elements.append(Paragraph(f"<b>Total: {purchase.total:,.2f}</b>", styles["Heading3"]))

    doc.build(elements)
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{purchase.ref_no}.pdf"'},
    )
