"""Purchasing: receive goods from a supplier, or return goods to them.

Receiving does three things in one step:
  1. adds the quantity to stock (converted to base units),
  2. auto-updates the product's cost price to what was actually just paid,
  3. flags products whose selling price no longer covers the new cost.
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models
from .database import get_db
from .deps import get_current_user
from .products import _get_or_create_category, _get_or_create_unit_type
from .templating import templates

router = APIRouter()

CENTS = Decimal("0.01")
COST_DP = Decimal("0.0001")


def _dec(value, default="0") -> Decimal:
    try:
        return Decimal(str(value).strip().replace(",", "") or default)
    except (InvalidOperation, AttributeError, ValueError):
        return Decimal(default)


def _money(value) -> Decimal:
    return _dec(value).quantize(CENTS, rounding=ROUND_HALF_UP)


def margin_alert(product: models.Product):
    """Return an alert dict when a product's selling price no longer clears cost."""
    if not product:
        return None
    cost = product.cost_price or Decimal("0")
    price = product.selling_price or Decimal("0")
    if cost <= 0:
        return None
    if price <= 0:
        return {"level": "danger", "message": "No selling price set."}
    if price < cost:
        return {"level": "danger", "message": f"Selling below cost by {(cost - price):.2f}"}
    if price == cost:
        return {"level": "warn", "message": "Selling price equals cost — zero margin."}
    return None


@router.get("/purchases/search")
def purchase_search(q: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Product lookup for the purchase form (includes current cost/selling price)."""
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
def list_purchases(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    purchases = db.query(models.Purchase).order_by(models.Purchase.id.desc()).limit(300).all()
    received = sum((p.total or Decimal("0")) for p in purchases if p.txn_type != "return")
    returned = sum((p.total or Decimal("0")) for p in purchases if p.txn_type == "return")
    return templates.TemplateResponse(
        "purchases/list.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "purchases": purchases, "received": received, "returned": returned},
    )


@router.get("/purchases/new", response_class=HTMLResponse)
def new_purchase(request: Request, supplier: int = 0, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
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

    product = models.Product(
        name=name,
        cost_price=Decimal("0"),
        selling_price=_money(data.get("selling_price") or 0),
        beginning_stock=Decimal("0"),
        stock_qty=Decimal("0"),
        is_active=True,
    )
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

    data = await request.json()
    lines = data.get("lines") or []
    if not lines:
        return JSONResponse({"ok": False, "error": "Add at least one item."}, status_code=400)

    txn_type = "return" if (data.get("txn_type") == "return") else "receive"
    supplier_id = data.get("supplier_id")
    supplier_id = int(supplier_id) if supplier_id else None
    if not supplier_id:
        return JSONResponse({"ok": False, "error": "Choose a supplier."}, status_code=400)

    purchase = models.Purchase(
        txn_type=txn_type,
        supplier_id=supplier_id,
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
            product.stock_qty = (product.stock_qty or Decimal("0")) + base_qty
            db.add(models.StockMovement(product_id=product.id, qty_base=base_qty, reason="purchase"))
            # Auto-update cost price to what we actually paid, per base unit.
            if unit_cost > 0:
                new_cost = (unit_cost / factor).quantize(CENTS, rounding=ROUND_HALF_UP)
                product.cost_price = new_cost

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


@router.get("/purchases/{purchase_id:int}", response_class=HTMLResponse)
def view_purchase(purchase_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    purchase = db.get(models.Purchase, purchase_id)
    if not purchase:
        return RedirectResponse("/purchases", status_code=302)

    # Which lines changed the cost, and which products now need a price review?
    rows = []
    for ln in purchase.lines:
        old = Decimal(str(ln.old_cost or 0))
        new = Decimal(str(ln.new_cost or 0))
        rows.append({
            "line": ln,
            "changed": purchase.txn_type != "return" and new != old,
            "increased": new > old,
            "diff": new - old,
            "alert": margin_alert(ln.product),
        })
    alerts = [r for r in rows if r["alert"]]
    return templates.TemplateResponse(
        "purchases/view.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "purchase": purchase, "rows": rows, "alerts": alerts},
    )
