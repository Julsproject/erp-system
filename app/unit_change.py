"""Change a product's base unit (e.g. Kg -> Box of 25 Kg) and convert
everything counted in the old unit in the same step.

Changing just the Unit Type label leaves the quantity in the old unit: 63.5
kg becomes "63.5 Box", and setting the cost per box then values it at 25x.
Here the quantity, cost, prices, units ladder and every history row that is
counted in base units (stock movements, sale / purchase / quotation lines,
stock counts, adjustments, month-end rollovers) are converted together by
the same factor. Each row's peso value stays exactly what it was, so the
conversion changes nothing in the books and posts no journal entry.

factor = how many OLD base units make ONE new base unit (25 for a 25 Kg box).
"""
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from . import audit, models, pricing
from .database import get_db
from .deps import get_current_user, is_staff
from .templating import templates

router = APIRouter()

ZERO = Decimal("0")
QTY_DP = Decimal("0.000001")
CENT = Decimal("0.01")


def _q(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(QTY_DP)


def _div(v, f):
    return None if v is None else (Decimal(str(v)) / f).quantize(QTY_DP)


def _factor_div(v, f):
    return None if v is None else (Decimal(str(v)) / f).quantize(Decimal("0.00000001"))


def _cost_mul(v, f):
    return None if v is None else (Decimal(str(v)) * f).quantize(CENT)


def change_base_unit(db: Session, product: models.Product, *, new_unit_name: str, factor: Decimal,
                     convert_cost: bool = True, convert_prices: bool = True,
                     already_converted_movement_ids=(), counterpart_factor: Decimal = None,
                     user=None, request=None) -> dict:
    """Convert `product` to `new_unit_name` where 1 new unit = `factor` old
    base units. Caller commits.

    convert_cost / convert_prices: False when they were already typed per
    the new unit. already_converted_movement_ids: movements already written
    in the new unit (left as they are, and not divided out of on-hand).
    counterpart_factor: overrides the computed replenish factor on a linked
    Open/Retail item (how many of its units one new unit opens into)."""
    from .products import _get_or_create_unit_type  # products imports half the app

    f = Decimal(str(factor))
    if f <= 0:
        raise ValueError("The conversion factor must be more than 0.")
    new_unit_name = (new_unit_name or "").strip()
    if not new_unit_name:
        raise ValueError("Enter the new base unit.")
    old_unit_name = product.unit_type.name if product.unit_type else "unit"
    skip = set(already_converted_movement_ids or ())
    before = {"unit": old_unit_name, "on_hand": str(product.total_qty), "cost": str(product.cost_price),
              "selling_price": str(product.selling_price)}

    # On hand: whatever's already in the new unit stays; the rest divides.
    skipped_qty = sum((Decimal(str(m.qty_base or 0)) for m in db.query(models.StockMovement)
                       .filter(models.StockMovement.id.in_(skip or {0}),
                               models.StockMovement.product_id == product.id)), ZERO)
    old_total = Decimal(str(product.total_qty or 0))
    new_total = ((old_total - skipped_qty) / f + skipped_qty).quantize(QTY_DP)
    product.beginning_stock = _div(product.beginning_stock, f)
    product.stock_qty = (new_total - product.beginning_stock).quantize(QTY_DP)
    product.reorder_level = _div(product.reorder_level, f)

    if convert_cost:
        product.cost_price = _cost_mul(product.cost_price, f)
    if convert_prices:
        product.selling_price = _cost_mul(product.selling_price, f)
    pricing.apply_to(product, product.cost_price, product.markup_pct, product.margin_pct)

    # Units ladder: every factor was in old base units. A unit that now IS
    # the base (factor 1, same name) would just duplicate it — drop it.
    for u in list(product.units):
        u.factor_to_base = _factor_div(u.factor_to_base, f)
        if u.factor_to_base == 1 and u.name.strip().lower() == new_unit_name.lower():
            db.delete(u)

    new_unit = _get_or_create_unit_type(db, new_unit_name)

    # Linked Open/Retail item that opens this one: 1 new unit opens into
    # (what 1 old unit opened into) x factor of its units.
    for cp in db.query(models.Product).filter(models.Product.replenish_from_id == product.id).all():
        if counterpart_factor is not None:
            cp.replenish_factor = Decimal(str(counterpart_factor))
        else:
            per_old = Decimal(str(cp.replenish_factor)) if (cp.replenish_factor and cp.unit_type_id != product.unit_type_id) else Decimal("1")
            cp.replenish_factor = (per_old * f).quantize(Decimal("0.00000001"))
    # This product is itself an Open/Retail item: fewer of its (bigger) units per source unit.
    if product.replenish_from_id and product.replenish_factor:
        product.replenish_factor = _factor_div(product.replenish_factor, f)

    product.unit_type = new_unit

    counts = {}
    mv = [m for m in db.query(models.StockMovement).filter(models.StockMovement.product_id == product.id).all()
          if m.id not in skip]
    for m in mv:
        m.qty_base = _div(m.qty_base, f)
        m.unit_cost = _cost_mul(m.unit_cost, f)   # value (qty x cost) stays exactly the same
    counts["stock movements"] = len(mv)

    rows = db.query(models.SaleLine).filter(models.SaleLine.product_id == product.id).all()
    for ln in rows:
        ln.unit_factor = _factor_div(ln.unit_factor, f)
        ln.unit_cost = _cost_mul(ln.unit_cost, f)
    counts["sale lines"] = len(rows)

    rows = db.query(models.QuotationLine).filter(models.QuotationLine.product_id == product.id).all()
    for ln in rows:
        ln.unit_factor = _factor_div(ln.unit_factor, f)
    counts["quotation lines"] = len(rows)

    rows = db.query(models.PurchaseLine).filter(models.PurchaseLine.product_id == product.id).all()
    for ln in rows:
        ln.unit_factor = _factor_div(ln.unit_factor, f)
        ln.old_cost = _cost_mul(ln.old_cost, f)
        ln.new_cost = _cost_mul(ln.new_cost, f)
    counts["purchase lines"] = len(rows)

    rows = db.query(models.StockCountLine).filter(models.StockCountLine.product_id == product.id).all()
    for ln in rows:
        ln.system_qty = _div(ln.system_qty, f)
        ln.counted_qty = _div(ln.counted_qty, f)
    counts["stock count lines"] = len(rows)

    rows = db.query(models.InventoryAdjustmentLine).filter(models.InventoryAdjustmentLine.product_id == product.id).all()
    for ln in rows:
        ln.unit_factor = _factor_div(ln.unit_factor, f)
        ln.qty_base = _div(ln.qty_base, f)
        ln.on_hand_before = _div(ln.on_hand_before, f)
        ln.old_cost = _cost_mul(ln.old_cost, f)
        ln.new_cost = _cost_mul(ln.new_cost, f)
    counts["adjustment lines"] = len(rows)

    rows = db.query(models.MonthEndRolloverLine).filter(models.MonthEndRolloverLine.product_id == product.id).all()
    for ln in rows:
        ln.qty_moved = _div(ln.qty_moved, f)
        ln.old_beginning = _div(ln.old_beginning, f)
        ln.new_beginning = _div(ln.new_beginning, f)
    counts["rollover lines"] = len(rows)

    after = {"unit": new_unit_name, "on_hand": str(product.total_qty), "cost": str(product.cost_price),
             "selling_price": str(product.selling_price)}
    audit.record(
        db, user=user, request=request, action="update", entity_type="product",
        entity_id=product.id, entity_label=product.name,
        summary=(f"Changed base unit of “{product.name}”: 1 {new_unit_name} = {f.normalize():f} {old_unit_name}; "
                 f"on hand {before['on_hand']} → {after['on_hand']}, cost ₱{before['cost']} → ₱{after['cost']}"),
        changes={"before": before, "after": after, "converted": counts},
    )
    return {"before": before, "after": after, "converted": counts}


@router.get("/products/{product_id:int}/change-unit", response_class=HTMLResponse)
def change_unit_form(product_id: int, request: Request, error: str = "",
                     db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/products", status_code=302)
    product = db.get(models.Product, product_id)
    if not product:
        return RedirectResponse("/products", status_code=302)
    counterpart = db.query(models.Product).filter(models.Product.replenish_from_id == product.id).first()
    unit_types = db.query(models.UnitType).order_by(models.UnitType.name).all()
    return templates.TemplateResponse("products/change_unit.html", {
        "request": request, "app_name": request.app.title, "user": user,
        "product": product, "counterpart": counterpart, "unit_types": unit_types, "error": error,
    })


@router.post("/products/{product_id:int}/change-unit")
def change_unit_submit(product_id: int, request: Request, new_unit: str = Form(""), factor: str = Form(""),
                       db: Session = Depends(get_db), user=Depends(get_current_user)):
    from urllib.parse import quote
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/products", status_code=302)
    product = db.get(models.Product, product_id, with_for_update=True)
    if not product:
        return RedirectResponse("/products", status_code=302)
    try:
        f = Decimal((factor or "").replace(",", "").strip())
    except Exception:  # noqa: BLE001
        f = Decimal("0")
    old = product.unit_type.name if product.unit_type else ""
    if (new_unit or "").strip().lower() == old.lower():
        return RedirectResponse(f"/products/{product_id}/change-unit?error={quote('That is already its base unit.')}", status_code=302)
    try:
        change_base_unit(db, product, new_unit_name=new_unit, factor=f, user=user, request=request)
    except ValueError as e:
        db.rollback()
        return RedirectResponse(f"/products/{product_id}/change-unit?error={quote(str(e))}", status_code=302)
    db.commit()
    return RedirectResponse(f"/products/{product_id}/stock-card", status_code=302)
