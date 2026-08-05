"""Physical Stock Count (cycle count): scan what's actually on the shelf and
reconcile against what the system thinks is on hand, with live variance —
instead of the manual scan-to-Excel-then-import round trip.

Only one count session is open at a time, store-wide, to keep this simple —
nothing scoped to a category/location yet. A count is delta-based: each
line snapshots system_qty the moment it's first scanned, and Complete
applies (counted - system_qty) as a signed stock movement, so it's still
correct even if sales happen elsewhere on the system while counting.
"""
import json
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import audit, models
from .database import get_db
from .deps import get_current_user, is_staff
from .products import ADJUSTMENT_REASON_LABELS, ADJUSTMENT_REASONS
from .templating import templates

router = APIRouter()

PAGE_SIZE = 15


def _dec(value, default="0") -> Decimal:
    try:
        return Decimal(str(value).strip().replace(",", "") or default)
    except (InvalidOperation, AttributeError, ValueError):
        return Decimal(default)


def _find_products(db: Session, q: str):
    """Same priority as POS: an exact barcode match wins outright (that's
    what a scan produces); otherwise fall back to a name search so counting
    still works for products with no barcode on file."""
    q = (q or "").strip()
    if not q:
        return []
    barcode_hit = (
        db.query(models.Product)
        .filter(models.Product.is_active.is_(True), models.Product.barcode == q)
        .first()
    )
    if barcode_hit:
        return [barcode_hit]
    return (
        db.query(models.Product)
        .filter(models.Product.is_active.is_(True), models.Product.name.ilike(f"%{q}%"))
        .order_by(models.Product.name)
        .limit(15)
        .all()
    )


def _line_dict(line: models.StockCountLine) -> dict:
    variance = Decimal(str(line.counted_qty or 0)) - Decimal(str(line.system_qty or 0))
    try:
        breakdown = json.loads(line.unit_breakdown) if line.unit_breakdown else {}
    except (ValueError, TypeError):
        breakdown = {}
    product = line.product
    units = []
    if product:
        base_name = product.unit_type.name if product.unit_type else "unit"
        units.append({"name": base_name, "factor": 1.0, "qty": breakdown.get(base_name, "")})
        for u in product.units:
            units.append({"name": u.name, "factor": float(u.factor_to_base), "qty": breakdown.get(u.name, "")})
    return {
        "id": line.id,
        "product_id": line.product_id,
        "name": line.product_name,
        "system_qty": float(line.system_qty or 0),
        "counted_qty": float(line.counted_qty or 0),
        "variance": float(variance),
        "units": units if len(units) > 1 else [],  # only worth showing when there's an actual ladder
    }


@router.get("/stock-count", response_class=HTMLResponse)
def stock_count_list(request: Request, page: int = 1, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    page = max(page, 1)
    open_count = db.query(models.StockCount).filter(models.StockCount.status == "open").first()
    past_q = db.query(models.StockCount).filter(models.StockCount.status != "open").order_by(models.StockCount.id.desc())
    total = past_q.count()
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, pages)
    past = past_q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    return templates.TemplateResponse(
        "stock_count/list.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "open_count": open_count, "past": past, "page": page, "pages": pages, "total": total},
    )


@router.post("/stock-count/start")
def stock_count_start(db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    existing = db.query(models.StockCount).filter(models.StockCount.status == "open").first()
    if existing:
        return RedirectResponse(f"/stock-count/{existing.id}", status_code=302)
    count = models.StockCount(status="open", created_by=user.id)
    db.add(count)
    db.flush()
    count.ref_no = f"SC-{count.id:06d}"
    db.commit()
    return RedirectResponse(f"/stock-count/{count.id}", status_code=302)


@router.get("/stock-count/{count_id:int}", response_class=HTMLResponse)
def stock_count_view(count_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    count = db.get(models.StockCount, count_id)
    if not count:
        return RedirectResponse("/stock-count", status_code=302)
    line_rows = (
        db.query(models.StockCountLine)
        .filter(models.StockCountLine.stock_count_id == count.id)
        .order_by(models.StockCountLine.product_name)
        .all()
    )
    lines = [_line_dict(l) for l in line_rows]
    variance_count = sum(1 for l in lines if l["counted_qty"] != l["system_qty"])
    return templates.TemplateResponse(
        "stock_count/session.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "count": count, "lines": lines, "variance_count": variance_count,
         "adjustment_reasons": ADJUSTMENT_REASONS},
    )


@router.post("/stock-count/{count_id:int}/scan")
async def stock_count_scan(count_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user or not is_staff(user):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    count = db.get(models.StockCount, count_id)
    if not count or count.status != "open":
        return JSONResponse({"ok": False, "error": "This count isn't open anymore."}, status_code=400)

    data = await request.json()
    add_qty = _dec(data.get("qty"), "1")

    # A disambiguation pick (from a previous ambiguous name search) sends the
    # product id directly instead of searching again.
    product_id = data.get("product_id")
    if product_id:
        product = db.get(models.Product, int(product_id))
        if not product or not product.is_active:
            return JSONResponse({"ok": False, "error": "That product isn't available anymore."}, status_code=404)
    else:
        q = (data.get("q") or "").strip()
        if not q:
            return JSONResponse({"ok": False, "error": "Scan or type something to search."}, status_code=400)
        matches = _find_products(db, q)
        if not matches:
            return JSONResponse({"ok": False, "error": f"No product matches “{q}”."}, status_code=404)
        if len(matches) > 1:
            return JSONResponse({"ok": False, "choices": [{"id": p.id, "name": p.name} for p in matches]})
        product = matches[0]
    line = (
        db.query(models.StockCountLine)
        .filter(models.StockCountLine.stock_count_id == count.id, models.StockCountLine.product_id == product.id)
        .first()
    )
    if not line:
        line = models.StockCountLine(
            stock_count_id=count.id, product_id=product.id, product_name=product.name,
            system_qty=Decimal(str(product.total_qty or 0)), counted_qty=Decimal("0"),
        )
        db.add(line)
    line.counted_qty = Decimal(str(line.counted_qty or 0)) + add_qty
    db.commit()
    return {"ok": True, "line": _line_dict(line)}


@router.get("/stock-count/{count_id:int}/search")
def stock_count_search(count_id: int, q: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Live typeahead as the scan box is typed into by hand (a barcode
    scanner still just types-and-Enters, unaffected by this) — same
    matching rules as an actual scan, via _find_products."""
    if not user or not is_staff(user):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    matches = _find_products(db, q)
    return {"products": [{"id": p.id, "name": p.name} for p in matches]}


@router.post("/stock-count/{count_id:int}/line/{line_id:int}/set")
async def stock_count_set_line(count_id: int, line_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user or not is_staff(user):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    count = db.get(models.StockCount, count_id)
    if not count or count.status != "open":
        return JSONResponse({"ok": False, "error": "This count isn't open anymore."}, status_code=400)
    line = db.get(models.StockCountLine, line_id)
    if not line or line.stock_count_id != count.id:
        return JSONResponse({"ok": False, "error": "Line not found."}, status_code=404)

    data = await request.json()
    try:
        new_qty = Decimal(str(data.get("counted_qty", "")).strip().replace(",", ""))
    except InvalidOperation:
        return JSONResponse({"ok": False, "error": "Enter a valid quantity."}, status_code=400)
    if new_qty < 0:
        return JSONResponse({"ok": False, "error": "Quantity can't be negative."}, status_code=400)
    line.counted_qty = new_qty
    line.unit_breakdown = None  # a plain-number edit overrides any per-unit breakdown
    db.commit()
    return {"ok": True, "line": _line_dict(line)}


@router.post("/stock-count/{count_id:int}/line/{line_id:int}/set-units")
async def stock_count_set_line_units(count_id: int, line_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Per-unit count entry — e.g. '2 FORWARD, 3 Elf, 1 Elf 1/2 physically on
    the shelf' — resolved into the one counted_qty (base units) everything
    else reads, so this is purely a friendlier way to arrive at that number."""
    if not user or not is_staff(user):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    count = db.get(models.StockCount, count_id)
    if not count or count.status != "open":
        return JSONResponse({"ok": False, "error": "This count isn't open anymore."}, status_code=400)
    line = db.get(models.StockCountLine, line_id)
    if not line or line.stock_count_id != count.id:
        return JSONResponse({"ok": False, "error": "Line not found."}, status_code=404)
    product = line.product
    if not product:
        return JSONResponse({"ok": False, "error": "Product not found."}, status_code=404)

    data = await request.json()
    raw = data.get("units") or {}
    if not isinstance(raw, dict):
        return JSONResponse({"ok": False, "error": "Invalid data."}, status_code=400)

    base_name = product.unit_type.name if product.unit_type else "unit"
    factor_by_name = {base_name: Decimal("1")}
    for u in product.units:
        factor_by_name[u.name] = Decimal(str(u.factor_to_base or 0))

    total = Decimal("0")
    breakdown = {}
    for name, val in raw.items():
        if name not in factor_by_name:
            continue
        qty = _dec(val, "0")
        if qty < 0:
            return JSONResponse({"ok": False, "error": f"Quantity for {name} can't be negative."}, status_code=400)
        if qty == 0 and str(val).strip() == "":
            continue  # skip untouched fields entirely, don't record a stray "0"
        breakdown[name] = str(val).strip()
        total += qty * factor_by_name[name]

    line.counted_qty = total
    line.unit_breakdown = json.dumps(breakdown) if breakdown else None
    db.commit()
    return {"ok": True, "line": _line_dict(line)}


@router.post("/stock-count/{count_id:int}/complete")
def stock_count_complete(count_id: int, request: Request, reason: str = Form("count_correction"),
                          db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    count = db.get(models.StockCount, count_id)
    if not count or count.status != "open":
        return RedirectResponse("/stock-count", status_code=302)

    reason_label = ADJUSTMENT_REASON_LABELS.get(reason, reason)
    for line in count.lines:
        variance = Decimal(str(line.counted_qty or 0)) - Decimal(str(line.system_qty or 0))
        if variance == 0:
            continue
        product = db.get(models.Product, line.product_id, with_for_update=True)
        if not product:
            continue
        before_qty = Decimal(str(product.stock_qty or 0))
        product.stock_qty = before_qty + variance
        unit_cost = Decimal(str(product.cost_price or 0))
        db.add(models.StockMovement(
            product_id=product.id, qty_base=variance, reason="stock_count", ref=count.ref_no,
            unit_cost=unit_cost, value=variance * unit_cost, note=reason_label,
        ))
        audit.record(
            db, user=user, request=request, action="stock_count", entity_type="product",
            entity_id=product.id, entity_label=product.name,
            summary=f"Stock count {count.ref_no}: counted {line.counted_qty:g}, system said {line.system_qty:g}",
            changes={"stock_qty": [str(before_qty), str(product.stock_qty)]},
        )

    count.status = "completed"
    count.completed_by = user.id
    count.completed_at = func.now()
    db.commit()
    return RedirectResponse(f"/stock-count/{count.id}", status_code=302)


@router.post("/stock-count/{count_id:int}/cancel")
def stock_count_cancel(count_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/pos", status_code=302)
    count = db.get(models.StockCount, count_id)
    if count and count.status == "open":
        count.status = "cancelled"
        count.completed_by = user.id
        count.completed_at = func.now()
        db.commit()
    return RedirectResponse("/stock-count", status_code=302)
