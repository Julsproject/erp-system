"""Purchasing: receive goods from a supplier, or return goods to them.

A receive-type purchase has a status lifecycle, same idea as Quotations:
  pending   -> a Purchase Order raised with a supplier; nothing in stock yet.
  confirmed -> the delivery physically arrived: stock is added and each
               product's cost price is updated to what was actually paid.
  paid      -> payment was later settled with the supplier; no stock effect.
A return has no staging — it removes stock immediately, same as before.
"""
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request, status as http_status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from . import models, pricing
from .database import get_db
from .deps import get_current_user, is_admin
from .products import _get_or_create_category, _get_or_create_unit_type
from .templating import templates

router = APIRouter()

CENTS = Decimal("0.01")
COST_DP = Decimal("0.0001")

PAYMENT_METHODS = [("cash", "Cash"), ("bank_transfer", "Bank Transfer"), ("cheque", "Cheque"), ("gcash", "GCash"), ("other", "Other")]
STATUS_LABELS = {"pending": "Pending", "confirmed": "Confirmed", "paid": "Paid", "cancelled": "Cancelled"}
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


def preview_margin_alert(product: models.Product, quoted_cost: Decimal):
    """Alert for a still-pending PO: what WOULD happen to margin once this
    quoted cost is applied at confirm time, even though it hasn't hit the
    product yet."""
    if not product:
        return None
    return _margin_check(quoted_cost, product.selling_price)


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

    out = []
    for p in products:
        base_unit = p.unit_type.name if p.unit_type else "Unit"
        units = [{"name": base_unit, "factor": 1.0}]
        for u in p.units:
            units.append({"name": u.name, "factor": float(u.factor_to_base or 1)})
        out.append({
            "id": p.id,
            "name": p.name,
            "base_unit": base_unit,
            "units": units,
            "cost_price": float(p.cost_price or 0),
            "selling_price": float(p.selling_price or 0),
            "on_hand": float(p.total_qty or 0),
        })
    return {"products": out}


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


@router.get("/purchases/new", response_class=HTMLResponse)
def new_purchase(request: Request, supplier: int = 0, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
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
         "categories": categories, "unit_types": unit_types},
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


@router.get("/purchases/receipts")
def list_receipts(supplier_id: int = 0, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Deliveries received from a supplier, for the 'return against' picker."""
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not is_admin(user):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if not supplier_id:
        return {"receipts": []}
    rows = (
        db.query(models.Purchase)
        .filter(
            models.Purchase.supplier_id == supplier_id,
            models.Purchase.txn_type == "receive",
            models.Purchase.status.in_(("confirmed", "paid")),
        )
        .order_by(models.Purchase.id.desc())
        .limit(30)
        .all()
    )
    return {
        "receipts": [
            {
                "id": p.id, "ref_no": p.ref_no,
                "date": p.created_at.strftime("%b %d, %Y") if p.created_at else "",
                "total": float(p.total or 0),
            }
            for p in rows
        ]
    }


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

    purchase = models.Purchase(
        txn_type=txn_type,
        # Returns take effect immediately (no staging, same as before). A
        # receive starts life as a pending Purchase Order — stock/cost only
        # change once it's confirmed as physically received.
        status="confirmed" if txn_type == "return" else "pending",
        confirmed_at=func.now() if txn_type == "return" else None,
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
        if unit_cost > 0:
            new_cost = (unit_cost / factor).quantize(CENTS, rounding=ROUND_HALF_UP)

        if txn_type == "return":
            # Goods going back to the supplier: take them out of stock, leave cost alone.
            product.stock_qty = (product.stock_qty or Decimal("0")) - base_qty
            db.add(models.StockMovement(product_id=product.id, qty_base=-base_qty, reason="purchase-return"))
            new_cost = old_cost
        # else: this is a Purchase Order — stock and cost are NOT touched here.
        # old_cost/new_cost below are just a preview of what confirming will do.

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
    purchase.ref_no = f"{prefix}-{purchase.id:06d}"
    db.commit()
    return {"ok": True, "purchase_id": purchase.id, "ref_no": purchase.ref_no}


@router.post("/purchases/{purchase_id:int}/confirm")
def confirm_purchase(purchase_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Mark a Purchase Order as physically received: add stock, update cost."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    purchase = db.get(models.Purchase, purchase_id)
    if not purchase or purchase.txn_type != "receive" or purchase.status != "pending":
        return RedirectResponse(f"/purchases/{purchase_id}", status_code=302)

    for line in purchase.lines:
        if not line.product_id:
            continue
        product = db.get(models.Product, line.product_id)
        if not product:
            continue
        base_qty = (line.qty or Decimal("0")) * (line.unit_factor or Decimal("1"))
        product.stock_qty = (product.stock_qty or Decimal("0")) + base_qty
        db.add(models.StockMovement(product_id=product.id, qty_base=base_qty, reason="purchase"))

        # Recompute against the product's cost right now (it may have moved
        # since this PO was drafted), then apply it.
        old_cost = Decimal(str(product.cost_price or 0))
        new_cost = old_cost
        if line.unit_cost and line.unit_cost > 0 and line.unit_factor:
            new_cost = (Decimal(str(line.unit_cost)) / Decimal(str(line.unit_factor))).quantize(CENTS, rounding=ROUND_HALF_UP)
            product.cost_price = new_cost
        line.old_cost = old_cost.quantize(COST_DP)
        line.new_cost = new_cost.quantize(COST_DP)

    purchase.status = "confirmed"
    purchase.confirmed_at = func.now()
    db.commit()
    return RedirectResponse(f"/purchases/{purchase_id}", status_code=http_status.HTTP_302_FOUND)


@router.post("/purchases/{purchase_id:int}/cancel")
def cancel_purchase(purchase_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Cancel a Purchase Order before it's received — nothing to undo yet."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    purchase = db.get(models.Purchase, purchase_id)
    if purchase and purchase.txn_type == "receive" and purchase.status == "pending":
        purchase.status = "cancelled"
        purchase.cancelled_at = func.now()
        db.commit()
    return RedirectResponse(f"/purchases/{purchase_id}", status_code=http_status.HTTP_302_FOUND)


@router.post("/purchases/{purchase_id:int}/pay")
async def mark_purchase_paid(
    purchase_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Record that this delivery has been paid for — no stock/cost effect."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_admin(user):
        return RedirectResponse("/pos", status_code=302)
    purchase = db.get(models.Purchase, purchase_id)
    if not purchase or purchase.txn_type != "receive" or purchase.status != "confirmed":
        return RedirectResponse(f"/purchases/{purchase_id}", status_code=302)

    form = await request.form()
    method = (form.get("method") or "cash").strip().lower()
    if method not in dict(PAYMENT_METHODS):
        method = "cash"

    if method == "cheque":
        # Issuing a post-dated cheque doesn't pay the supplier yet — the
        # purchase stays "confirmed" until the cheque actually clears.
        raw_date = (form.get("cheque_date") or "").strip()
        try:
            cheque_date = date.fromisoformat(raw_date)
        except ValueError:
            return RedirectResponse(
                f"/purchases/{purchase_id}?error=Enter+a+valid+cheque+date.", status_code=302
            )
        purchase.payment_method = "cheque"
        pdc = models.PostDatedCheque(
            direction="issued", amount=purchase.total,
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

    purchase.payment_method = method
    purchase.paid_at = func.now()
    purchase.status = "paid"
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

    # Which lines changed (or, while still pending, would change) the cost,
    # and which products now need — or would need — a price review?
    rows = []
    for ln in purchase.lines:
        old = Decimal(str(ln.old_cost or 0))
        new = Decimal(str(ln.new_cost or 0))
        if purchase.status == "pending":
            alert = preview_margin_alert(ln.product, new)
        else:
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
         "purchase": purchase, "rows": rows, "alerts": alerts, "methods": PAYMENT_METHODS,
         "linked_returns": linked_returns, "error": error, "pending_pdc": pending_pdc},
    )
