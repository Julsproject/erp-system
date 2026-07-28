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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from . import audit, models, pricing
from .database import get_db
from .deps import get_current_user, is_admin
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
PAGE_SIZE = 20


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
    if not is_admin(user):
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
    if not is_admin(user):
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
    if not is_admin(user):
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


@router.get("/purchases/payables", response_class=HTMLResponse)
def list_payables(
    request: Request,
    q: str = "",
    supplier_id: int = 0,
    page: int = 1,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """What we owe suppliers: every confirmed-but-unpaid receiving, with aging.

    A 'confirmed' receive-type purchase IS the payable — goods and cost
    already moved, payment hasn't; 'pending' isn't owed yet (nothing
    received), 'paid' no longer is. Mirrors Sales -> Receivables, except
    there's no partial-settlement ledger here (a purchase is paid in full,
    not collected against over time), so the outstanding amount is just the
    purchase total.
    """
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)

    q = (q or "").strip()
    page = max(page, 1)
    today = date.today()
    horizon = today + timedelta(days=DUE_SOON_DAYS)

    query = (
        db.query(models.Purchase)
        .filter(models.Purchase.txn_type == "receive", models.Purchase.status == "confirmed")
    )
    if supplier_id:
        query = query.filter(models.Purchase.supplier_id == supplier_id)
    if q:
        like = f"%{q}%"
        query = query.outerjoin(models.Supplier, models.Purchase.supplier_id == models.Supplier.id).filter(
            or_(models.Purchase.ref_no.ilike(like), models.Purchase.invoice_no.ilike(like), models.Supplier.name.ilike(like))
        )

    total_count, total_owed = query.with_entities(
        func.count(models.Purchase.id), func.coalesce(func.sum(models.Purchase.total), 0)
    ).one()
    pages = max((total_count + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, pages)
    rows = (
        query.order_by(func.coalesce(models.Purchase.due_date, models.Purchase.confirmed_at).asc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
        .all()
    )

    overdue_count = sum(1 for p in rows if p.due_date and p.due_date < today)
    due_soon_count = sum(1 for p in rows if p.due_date and today <= p.due_date <= horizon)

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


@router.get("/purchases/new", response_class=HTMLResponse)
def new_purchase(
    request: Request, supplier: int = 0, return_against: int = 0,
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
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
    if not is_admin(user):
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
    if not is_admin(user):
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
        due_date = date.today() + timedelta(days=int(days))

    purchase = models.Purchase(
        txn_type=txn_type,
        status="confirmed" if (txn_type == "return" or is_payable) else "paid",
        confirmed_at=func.now(),
        paid_at=func.now() if (txn_type == "receive" and not is_payable) else None,
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
        product = db.get(models.Product, int(ln["product_id"])) if ln.get("product_id") else None
        if not product:
            continue
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
            # Goods going back to the supplier: take them out of stock, leave cost alone.
            product.stock_qty = (product.stock_qty or Decimal("0")) - base_qty
            db.add(models.StockMovement(product_id=product.id, qty_base=-base_qty, reason="purchase-return"))
        else:
            # Receiving: blend into the weighted-average cost BEFORE stock_qty
            # moves — the blend needs the pre-receipt quantity as its weight —
            # then add the stock, immediately (no separate confirm step).
            if unit_cost > 0:
                new_cost = _weighted_avg_cost(product, base_qty, unit_cost / factor)
                product.cost_price = new_cost
            product.stock_qty = (product.stock_qty or Decimal("0")) + base_qty
            db.add(models.StockMovement(product_id=product.id, qty_base=base_qty, reason="purchase"))

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


@router.post("/purchases/{purchase_id:int}/mark-paid")
async def mark_purchase_paid(purchase_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Settle a payable — the only step left for a receive that was logged
    as unpaid at the time. Stock and cost already moved when it was created;
    this just closes out what's owed."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    purchase = db.get(models.Purchase, purchase_id)
    if not purchase or purchase.txn_type != "receive" or purchase.status != "confirmed":
        return RedirectResponse(f"/purchases/{purchase_id}", status_code=http_status.HTTP_302_FOUND)

    form = await request.form()
    method = (form.get("payment_method") or "cash").strip().lower()
    if method not in dict(PAYMENT_METHODS):
        method = "cash"

    if method == "cheque":
        raw_date = (form.get("cheque_date") or "").strip()
        try:
            cheque_date = date.fromisoformat(raw_date)
        except ValueError:
            return RedirectResponse(
                f"/purchases/{purchase_id}?error=Enter+a+valid+cheque+date.", status_code=http_status.HTTP_302_FOUND
            )
        db.add(models.PostDatedCheque(
            direction="issued", amount=purchase.total,
            bank=(form.get("bank") or "").strip() or None,
            cheque_no=(form.get("cheque_no") or "").strip() or None,
            cheque_date=cheque_date,
            purchase_id=purchase.id, supplier_id=purchase.supplier_id,
            created_by=user.id,
        ))

    purchase.status = "paid"
    purchase.payment_method = method
    purchase.paid_at = func.now()
    audit.record(
        db, user=user, request=request, action="update", entity_type="purchase",
        entity_id=purchase.id, entity_label=purchase.ref_no,
        summary=f"Marked {purchase.ref_no} as paid via {dict(PAYMENT_METHODS)[method]}",
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
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    purchase = db.get(models.Purchase, purchase_id)
    if not purchase or purchase.status == "cancelled":
        return RedirectResponse(f"/purchases/{purchase_id}", status_code=http_status.HTTP_302_FOUND)

    for line in purchase.lines:
        if not line.product_id:
            continue
        product = db.get(models.Product, line.product_id)
        if not product:
            continue
        base_qty = (line.qty or Decimal("0")) * (line.unit_factor or Decimal("1"))
        # Undo whichever direction this line originally moved stock.
        delta = -base_qty if purchase.txn_type == "receive" else base_qty
        product.stock_qty = (product.stock_qty or Decimal("0")) + delta
        db.add(models.StockMovement(product_id=product.id, qty_base=delta, reason="purchase-cancelled"))

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
    if not is_admin(user):
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

    return templates.TemplateResponse(
        "purchases/view.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "purchase": purchase, "rows": rows, "alerts": alerts,
         "linked_returns": linked_returns, "error": error, "pending_pdc": pending_pdc,
         "today": date.today(), "payment_methods": PAYMENT_METHODS},
    )
