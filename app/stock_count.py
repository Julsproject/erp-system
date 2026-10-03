"""Physical Stock Count (cycle count): scan what's actually on the shelf and
reconcile against what the system thinks is on hand, with live variance —
instead of the manual scan-to-Excel-then-import round trip.

Only one count session is open at a time, store-wide, to keep this simple —
nothing scoped to a category/location yet. A count is delta-based: each
line snapshots system_qty the moment it's first scanned, and Complete
applies (counted - system_qty) as a signed stock movement, so it's still
correct even if sales happen elsewhere on the system while counting.
"""
import io
import json
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import audit, models
from .database import SessionLocal, get_db
from .deps import get_current_user, is_floor_staff, is_staff, safe_back_url, url_with
from .pos import MANILA, _apply_stock_count_correction
from .products import ADJUSTMENT_REASON_LABELS, ADJUSTMENT_REASONS, _parse_upload
from .search_utils import multi_word_ilike
from .templating import templates

router = APIRouter()

PAGE_SIZE = 15

# Throttles the effective-date rebase check to at most once per real day per
# process — same pattern as products._last_rollover_check_day. Not
# persisted; a restart just means the next request re-checks, which is
# harmless since _apply_effective_date_rebase only ever runs once per count
# (guarded by effective_applied_at, not by this).
_last_effective_date_check_day = None


def movement_ref_date(db: Session, m: models.StockMovement) -> date:
    """The transaction (reference) date a stock movement belongs to, in
    Manila time. Almost every movement is already stamped with its
    transaction's own date (backdated sales/deliveries, edits, repacks) —
    except the few that undo an earlier record, which are stamped whenever
    someone clicked them: a void/un-void belongs to its sale's date, a
    cancelled purchase to the purchase's date, a double-deduction correction
    to the movement it corrects."""
    stamp = m.created_at
    if m.reason in ("void", "unvoid") and m.ref:
        display = func.concat(func.coalesce(models.Sale.receipt_type, ""), models.Sale.invoice_no)
        sale = db.query(models.Sale).filter(display == m.ref).order_by(models.Sale.id.desc()).first()
        stamp = sale.created_at if sale and sale.created_at else stamp
    elif m.reason == "purchase-cancelled" and m.ref:
        purchase = db.query(models.Purchase).filter(models.Purchase.ref_no == m.ref).first()
        stamp = purchase.created_at if purchase and purchase.created_at else stamp
    elif m.corrects_movement_id:
        original = db.get(models.StockMovement, m.corrects_movement_id)
        stamp = original.created_at if original and original.created_at else stamp
    return stamp.astimezone(MANILA).date()


def qty_dated_after(db: Session, product_id: int, cutoff: date) -> Decimal:
    """Net stock movement for a product dated (by reference date — see
    movement_ref_date) after `cutoff`, however late it was encoded. Stock
    count corrections are excluded; they aren't transactions."""
    rows = (
        db.query(models.StockMovement)
        .filter(models.StockMovement.product_id == product_id, models.StockMovement.reason != "stock_count")
        .all()
    )
    return sum((Decimal(str(m.qty_base or 0)) for m in rows if movement_ref_date(db, m) > cutoff), Decimal("0"))


def _default_effective_date(from_date: date) -> date:
    """The 1st of the month after from_date — the default Effective Date
    offered when a count completes, editable by admin/manager before or
    after."""
    if from_date.month == 12:
        return date(from_date.year + 1, 1, 1)
    return date(from_date.year, from_date.month + 1, 1)


def _apply_effective_date_rebase(db: Session, count: models.StockCount) -> int:
    """Folds each of this count's covered products' current Stocks Qty (the
    full net movement since the count — sales, purchases, the count's own
    variance correction, whatever ran up since then, positive or negative)
    into Actual Beginning, and resets Stocks Qty to 0 — exactly what
    Month-End Rollover already does for every product on the calendar
    boundary (see products._run_month_end_rollover), just scoped to this
    count's products and triggered on the date Accounting chose for it
    instead. This is what makes Actual Beginning move at all — nothing else
    (not a sale, not the count's own completion) ever touches it directly.

    Lines unticked on the Counted Items list (include_in_beginning False)
    are skipped — that product's Actual Beginning is left for the regular
    month-end rollover instead."""
    product_ids = {l.product_id for l in count.lines if l.include_in_beginning}
    rolled = 0
    for pid in product_ids:
        product = db.get(models.Product, pid, with_for_update=True)
        if not product:
            continue
        old_beginning = Decimal(str(product.beginning_stock or 0))
        old_stock = Decimal(str(product.stock_qty or 0))
        if old_stock == 0:
            continue
        new_beginning = old_beginning + old_stock
        product.beginning_stock = new_beginning
        product.stock_qty = Decimal("0")
        rolled += 1
        db.add(models.MonthEndRolloverLine(
            product_id=product.id, product_name=product.name,
            period=count.effective_date.strftime("%Y-%m"),
            qty_moved=old_stock, old_beginning=old_beginning, new_beginning=new_beginning,
        ))
    count.effective_applied_at = func.now()
    if rolled:
        audit.record(
            db, action="update", entity_type="stock_count", entity_id=count.id,
            entity_label=count.ref_no,
            summary=f"Effective-date rebase for {count.ref_no} ({count.effective_date}): "
                    f"{rolled} product(s) — Stocks Qty folded into Actual Beginning",
        )
    return rolled


def maybe_apply_effective_date_rebases() -> None:
    """Piggybacked on normal page loads (see products.maybe_run_month_end_rollover
    for the same pattern) — applies any completed Stock Count whose
    Effective Date has arrived. Cheap in-memory guard skips the DB check
    after the first hit each day; the real once-ever-per-count guard is
    effective_applied_at, so this can never double-apply even across
    restarts or multiple app instances."""
    global _last_effective_date_check_day
    today = date.today()
    if _last_effective_date_check_day == today:
        return
    _last_effective_date_check_day = today
    db = SessionLocal()
    try:
        due = (
            db.query(models.StockCount)
            .filter(
                models.StockCount.status == "completed",
                models.StockCount.effective_date.isnot(None),
                models.StockCount.effective_date <= today,
                models.StockCount.effective_applied_at.is_(None),
            )
            .all()
        )
        for count in due:
            _apply_effective_date_rebase(db, count)
        if due:
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

# --- Bulk import (Excel / CSV) — fill in a count sheet instead of scanning
# every item by hand. Columns read by header name, like the Products import.
# Anything NOT listed here is treated as a per-unit column named after one of
# the product's own units (Sack, Elf Load, ...), so the reference-only columns
# below must be mapped even though nothing reads their value — otherwise a
# filled "Unit" cell would be misread as a unit quantity.
HEADER_MAP_COUNT = {
    "barcode": "barcode", "bar code": "barcode", "upc": "barcode", "ean": "barcode",
    "product name": "name", "name": "name", "product": "name",
    "shelf": "shelf_ref",
    "rename to": "rename_to", "renamed to": "rename_to", "new name": "rename_to", "rename": "rename_to",
    "counted qty": "counted", "counted": "counted", "count": "counted", "qty": "counted", "quantity": "counted",
    "unit": "unit_label", "base unit": "unit_label",
    "system qty (reference only)": "system_ref", "system qty": "system_ref", "system": "system_ref",
}
FIELDS_COUNT = ["barcode", "name", "shelf_ref", "rename_to", "counted", "unit_label", "system_ref"]
BASE_HEADERS_COUNT = ["Barcode", "Product Name", "Shelf", "Rename To", "Unit", "System Qty (reference only)", "Counted Qty"]


# Cells the count sheet writes as "not applicable" for a unit this product
# doesn't have. They read as filled-in text, so they'd otherwise come back on
# re-import looking like a quantity.
_BLANK_CELLS = {"", "—", "–", "-", "n/a", "na"}


def _is_blank_cell(value) -> bool:
    return str(value or "").strip().lower() in _BLANK_CELLS


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
        .filter(models.Product.is_active.is_(True), multi_word_ilike(models.Product.name, q))
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
        "shelf": (product.shelf.name if product and product.shelf else None),
        "include": line.include_in_beginning is not False,
    }


def _classify_count_import_rows(db: Session, count: models.StockCount, numbered_rows):
    """Read-only pass: match each row to a product (barcode wins, else exact
    name), skipping rows with nothing counted (a count sheet is filled in
    gradually, so most rows may still be blank).

    A row can express its count either as a plain base-unit number in the
    Counted Qty column, or spread across the per-unit columns (Sack, Elf
    Load, ...) — or both, which simply add up. Every unit is converted to
    base units via factor_to_base, exactly like the per-unit entry in the
    scan UI, because stock is stored as one base-unit number."""
    existing_line_by_product = {l.product_id: l for l in count.lines}
    classified = []
    for line_no, record in numbered_rows:
        barcode = (record["barcode"] or "").strip()
        name = (record["name"] or "").strip()
        counted_raw = (record["counted"] or "").strip()
        extra = {k: v for k, v in (record.get("_extra") or {}).items() if not _is_blank_cell(v)}
        if not barcode and not name:
            continue
        if not counted_raw and not extra:
            classified.append({"line_no": line_no, "name": name or barcode, "action": "blank"})
            continue

        product = None
        if barcode:
            product = (
                db.query(models.Product)
                .filter(models.Product.is_active.is_(True), models.Product.barcode == barcode)
                .first()
            )
        if not product and name:
            product = (
                db.query(models.Product)
                .filter(models.Product.is_active.is_(True), func.lower(models.Product.name) == name.lower())
                .first()
            )
        if not product:
            classified.append({
                "line_no": line_no, "name": name or barcode, "action": "error",
                "message": f"No active product matches {'barcode ' + barcode if barcode else 'name “' + name + '”'}.",
            })
            continue

        # Optional "Rename To" column — a counter walking the shelf can fix a
        # typo or naming-convention slip right on the sheet instead of a
        # separate trip to Inventory. Blank (or absent, on an older sheet
        # downloaded before this column existed — see FIELDS_COUNT/cell()
        # returning "" for a missing column) just means no rename requested.
        rename_to = (record.get("rename_to") or "").strip()
        if rename_to and rename_to.lower() == product.name.strip().lower():
            rename_to = ""  # retyped the same name — nothing to do
        if rename_to:
            clash = (
                db.query(models.Product.id)
                .filter(
                    models.Product.is_active.is_(True),
                    func.lower(models.Product.name) == rename_to.lower(),
                    models.Product.id != product.id,
                )
                .first()
            )
            if clash:
                classified.append({
                    "line_no": line_no, "name": product.name, "action": "error",
                    "message": f"Can't rename to “{rename_to}” — another active product already has that name.",
                })
                continue

        base_name = product.unit_type.name if product.unit_type else "unit"
        factor_by_key = {base_name.strip().lower(): (base_name, Decimal("1"))}
        for u in product.units:
            factor_by_key[u.name.strip().lower()] = (u.name, Decimal(str(u.factor_to_base or 0)))

        total = Decimal("0")
        breakdown = {}
        err = None
        for raw_label, raw_val in [(base_name, counted_raw)] + list(extra.items()):
            val = str(raw_val or "").strip()
            if not val:
                continue
            hit = factor_by_key.get(raw_label.strip().lower())
            if not hit:
                # A per-unit column that this particular product doesn't have
                # is normal — the sheet carries one column per unit found
                # across ALL products, so most rows leave most of them blank.
                # A *filled* one, though, is a real mistake worth reporting.
                err = f"“{raw_label}” isn't a unit of {product.name}."
                break
            unit_name, factor = hit
            try:
                qty = Decimal(val.replace(",", ""))
            except InvalidOperation:
                err = f"“{val}” isn't a valid quantity for {unit_name}."
                break
            if qty < 0:
                err = f"Quantity for {unit_name} can't be negative."
                break
            total += qty * factor
            breakdown[unit_name] = val
        if err:
            classified.append({"line_no": line_no, "name": product.name, "action": "error", "message": err})
            continue

        classified.append({
            "line_no": line_no, "name": product.name, "action": "set",
            "product": product, "counted": total, "rename_to": rename_to or None,
            # Only worth recording a breakdown when more than the base unit was
            # used — a plain number stays a plain number, same as a typed edit.
            "breakdown": breakdown if len(breakdown) > 1 or (breakdown and base_name not in breakdown) else None,
            "line": existing_line_by_product.get(product.id),
        })
    return classified


def _run_count_import(db: Session, count: models.StockCount, classified, assign_shelf=None, user=None, request=None):
    """Apply each matched row's Counted Qty as a line's new count — same
    effect as typing it into the Counted field by hand. A later row for the
    same product in the same file simply overwrites an earlier one.

    assign_shelf, if given, is set on any counted product that doesn't
    already have a shelf — never overwrites an existing assignment. Meant
    for exactly this workflow: download the count sheet for one shelf,
    walk it, count everything including items that were never assigned a
    shelf before, and have counting them also be what puts them on the map."""
    applied = skipped = shelved = renamed = 0
    errors = []
    lines_by_product = {}
    # What the sheet actually put into the count, per product — shown back
    # after the upload so nothing goes in unnoticed.
    summary_by_product = {}
    for item in classified:
        line_no, name, action = item["line_no"], item["name"], item["action"]
        if action == "blank":
            skipped += 1
            continue
        if action == "error":
            errors.append({"row": line_no, "name": name, "message": item["message"]})
            continue
        product = item["product"]
        rename_to = item.get("rename_to")
        if rename_to:
            old_name = product.name
            product.name = rename_to
            audit.record(
                db, user=user, request=request, action="update", entity_type="product",
                entity_id=product.id, entity_label=product.name,
                summary=f"Renamed “{old_name}” → “{rename_to}” (via stock count import)",
                changes={"name": [old_name, rename_to]},
            )
            renamed += 1
        line = lines_by_product.get(product.id) or item["line"]
        if product.id not in summary_by_product:
            summary_by_product[product.id] = {
                "status": "updated" if line else "new",
                "previous": Decimal(str(line.counted_qty or 0)) if line else None,
            }
        if not line:
            line = models.StockCountLine(
                stock_count_id=count.id, product_id=product.id, product_name=product.name,
                system_qty=Decimal(str(product.total_qty or 0)), counted_qty=Decimal("0"),
            )
            db.add(line)
        else:
            line.product_name = product.name
        line.counted_qty = item["counted"]
        line.unit_breakdown = json.dumps(item["breakdown"]) if item.get("breakdown") else None
        lines_by_product[product.id] = line
        if assign_shelf and product.shelf_id is None:
            product.shelf = assign_shelf
            shelved += 1
        applied += 1
    db.commit()
    items = []
    for pid, line in lines_by_product.items():
        s = summary_by_product[pid]
        system = Decimal(str(line.system_qty or 0))
        counted = Decimal(str(line.counted_qty or 0))
        items.append({
            "name": line.product_name, "status": s["status"], "previous": s["previous"],
            "unchanged": s["previous"] is not None and s["previous"] == counted,
            "system": system, "counted": counted, "variance": counted - system,
            "unit": line.product.unit_type.name if line.product and line.product.unit_type else "",
        })
    # Variances first (biggest first), then the rest by name.
    items.sort(key=lambda i: (i["variance"] == 0, -abs(i["variance"]), i["name"].lower()))
    return {
        "applied": applied, "skipped": skipped, "errors": errors, "shelved": shelved, "renamed": renamed,
        "assign_shelf_name": assign_shelf.name if assign_shelf else None,
        "total": len(classified), "items": items,
        "new_count": sum(1 for i in items if i["status"] == "new"),
        "variance_count": sum(1 for i in items if i["variance"] != 0),
    }


def _uncounted_negatives(db: Session, count: models.StockCount):
    """Active products sitting at negative on-hand that aren't in this count.

    Negative stock is normal while a backlog of past sales is being encoded
    against an inventory that was never opened — but by the time a count is
    completed, every one of those should have been physically counted and
    corrected. Anything still negative and *not* in the count is a product
    nobody put eyes on, and completing without it leaves that negative on the
    books indefinitely. Surfaced as a warning (not a block) — a shop may
    legitimately be counting only one shelf.
    """
    counted_ids = {
        pid for (pid,) in db.query(models.StockCountLine.product_id)
        .filter(models.StockCountLine.stock_count_id == count.id).all()
    }
    on_hand = func.coalesce(models.Product.beginning_stock, 0) + func.coalesce(models.Product.stock_qty, 0)
    q = (
        db.query(models.Product)
        .filter(models.Product.is_active.is_(True), on_hand < 0)
        .order_by(on_hand, models.Product.name)
    )
    if counted_ids:
        q = q.filter(~models.Product.id.in_(counted_ids))
    return q.all()


def _count_url(count_id, back="", **params):
    """A count's page, keeping the page it was opened from (`back`) so a
    save that lands back on it keeps its Back link."""
    return url_with(f"/stock-count/{count_id}", back=safe_back_url(back, ""), **params)


@router.get("/stock-count", response_class=HTMLResponse)
def stock_count_list(
    request: Request, page: int = 1, bulk_msg: int = 0, bulk_error: str = "",
    missing_effective: int = 0,
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_floor_staff(user):
        return RedirectResponse("/pos", status_code=302)
    page = max(page, 1)
    open_count = db.query(models.StockCount).filter(models.StockCount.status == "open").first()
    past_q = db.query(models.StockCount).filter(models.StockCount.status != "open")
    # Completed counts with no Effective Date set yet — nothing will ever
    # move their Actual Beginning until someone (admin/manager) picks a
    # date, so this is the "still needs attention" view of the same list.
    missing_effective_count = past_q.filter(
        models.StockCount.status == "completed", models.StockCount.effective_date.is_(None)
    ).count()
    if missing_effective:
        past_q = past_q.filter(
            models.StockCount.status == "completed", models.StockCount.effective_date.is_(None)
        )
    past_q = past_q.order_by(models.StockCount.id.desc())
    total = past_q.count()
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, pages)
    past = past_q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    return templates.TemplateResponse(
        "stock_count/list.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "open_count": open_count, "past": past, "page": page, "pages": pages, "total": total,
         "bulk_msg": bulk_msg, "bulk_error": bulk_error,
         "missing_effective": missing_effective, "missing_effective_count": missing_effective_count},
    )


@router.post("/stock-count/start")
def stock_count_start(db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_floor_staff(user):
        return RedirectResponse("/pos", status_code=302)
    existing = db.query(models.StockCount).filter(models.StockCount.status == "open").first()
    if existing:
        return RedirectResponse(f"/stock-count/{existing.id}", status_code=302)
    count = models.StockCount(status="open", created_by=user.id, count_date=date.today())
    db.add(count)
    db.flush()
    count.ref_no = f"SC-{count.id:06d}"
    db.commit()
    return RedirectResponse(f"/stock-count/{count.id}", status_code=302)


@router.get("/stock-count/{count_id:int}", response_class=HTMLResponse)
def stock_count_view(
    count_id: int, request: Request, date_error: str = "", back: str = "",
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    """`back` is the page the count was opened from (the list, Counted
    Items, a Stock Card)."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_floor_staff(user):
        return RedirectResponse("/pos", status_code=302)
    back = safe_back_url(back, "/stock-count")
    count = db.get(models.StockCount, count_id)
    if not count:
        return RedirectResponse(back, status_code=302)
    line_rows = (
        db.query(models.StockCountLine)
        .filter(models.StockCountLine.stock_count_id == count.id)
        .order_by(models.StockCountLine.product_name)
        .all()
    )
    lines = [_line_dict(l) for l in line_rows]
    variance_count = sum(1 for l in lines if l["counted_qty"] != l["system_qty"])
    shelves = db.query(models.Shelf).order_by(models.Shelf.name).all()
    missing_negatives = _uncounted_negatives(db, count) if count.status == "open" else []
    date_error_msg = (
        "Enter a valid date." if date_error == "invalid"
        else "Count date can't be in the future." if date_error == "future"
        else "That count's effective date has already been applied — it can't be changed anymore." if date_error == "applied"
        else ""
    )
    can_edit_count_date = is_floor_staff(user) if count.status == "open" else is_staff(user)
    can_edit_effective_date = (
        count.status == "completed" and not count.effective_applied_at and is_staff(user)
    )
    return templates.TemplateResponse(
        "stock_count/session.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "count": count, "lines": lines, "variance_count": variance_count,
         "adjustment_reasons": ADJUSTMENT_REASONS, "shelves": shelves,
         "missing_negatives": missing_negatives, "date_error": date_error_msg,
         "can_edit_count_date": can_edit_count_date,
         "can_edit_effective_date": can_edit_effective_date,
         "today_iso": date.today().isoformat(), "back": back},
    )


@router.post("/stock-count/{count_id:int}/set-count-date")
def stock_count_set_date(
    count_id: int, request: Request, count_date: str = Form(""), back: str = Form(""),
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    """The date the physical count actually happened — see the count_date
    column comment on the model. Settable by anyone who can work an open
    count (same as counting itself); once the count is completed or
    cancelled, the number it corrected is already final, so re-attributing
    which date that correction belongs to is admin/manager territory, and
    logged either way."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    count = db.get(models.StockCount, count_id)
    if not count:
        return RedirectResponse("/stock-count", status_code=302)
    allowed = is_floor_staff(user) if count.status == "open" else is_staff(user)
    if not allowed:
        return RedirectResponse(_count_url(count_id, back), status_code=302)
    try:
        new_date = date.fromisoformat((count_date or "").strip())
    except ValueError:
        return RedirectResponse(_count_url(count_id, back, date_error="invalid"), status_code=302)
    if new_date > date.today():
        return RedirectResponse(_count_url(count_id, back, date_error="future"), status_code=302)
    old_date = count.count_date
    if new_date != old_date:
        count.count_date = new_date
        audit.record(
            db, user=user, request=request, action="update", entity_type="stock_count",
            entity_id=count.id, entity_label=count.ref_no,
            summary=f"Changed count date for {count.ref_no}: {old_date or '—'} → {new_date}",
            changes={"count_date": [str(old_date) if old_date else None, str(new_date)]},
        )
        db.commit()
    return RedirectResponse(_count_url(count_id, back), status_code=302)


@router.post("/stock-count/{count_id:int}/set-effective-date")
def stock_count_set_effective_date(
    count_id: int, request: Request, effective_date: str = Form(""), back: str = Form(""),
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    """When this count's result becomes the new Actual Beginning — see the
    effective_date column comment on the model. Admin/manager only (this
    decides the exact date Actual Beginning moves, which is Accounting's
    call), and only while the count is completed but not yet applied — once
    the fold has actually run (effective_applied_at set), changing the date
    here wouldn't undo or redo it, so editing is blocked past that point."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse(_count_url(count_id, back), status_code=302)
    count = db.get(models.StockCount, count_id)
    if not count or count.status != "completed":
        return RedirectResponse(safe_back_url(back, "/stock-count"), status_code=302)
    if count.effective_applied_at:
        return RedirectResponse(_count_url(count_id, back, date_error="applied"), status_code=302)
    try:
        new_date = date.fromisoformat((effective_date or "").strip())
    except ValueError:
        return RedirectResponse(_count_url(count_id, back, date_error="invalid"), status_code=302)
    old_date = count.effective_date
    if new_date != old_date:
        count.effective_date = new_date
        audit.record(
            db, user=user, request=request, action="update", entity_type="stock_count",
            entity_id=count.id, entity_label=count.ref_no,
            summary=f"Changed effective date for {count.ref_no}: {old_date or '—'} → {new_date}",
            changes={"effective_date": [str(old_date) if old_date else None, str(new_date)]},
        )
        db.commit()
    return RedirectResponse(_count_url(count_id, back), status_code=302)


@router.post("/stock-count/bulk-set-effective-date")
def stock_count_bulk_set_effective_date(
    request: Request, count_ids: list[str] = Form([]), effective_date: str = Form(""),
    missing_effective: int = Form(0), back: str = Form(""),
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    """Same rule and permission as the single set-effective-date route,
    applied to many completed counts in one go — a shop that runs several
    count sessions (one per shelf/area, say) covering one period shouldn't
    have to open each one individually just to point them at the same
    Effective Date. Silently skips anything not eligible (not completed yet,
    or already applied) rather than erroring the whole batch out."""
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return RedirectResponse("/stock-count", status_code=302)
    # `back`: the list exactly as it was (filter and page).
    back = safe_back_url(back, "/stock-count?missing_effective=1" if missing_effective else "/stock-count")
    try:
        new_date = date.fromisoformat((effective_date or "").strip())
    except ValueError:
        return RedirectResponse(url_with(back, bulk_error="invalid", bulk_msg=None), status_code=302)

    updated = 0
    for cid in {int(i) for i in count_ids if i.isdigit()}:
        count = db.get(models.StockCount, cid)
        if not count or count.status != "completed" or count.effective_applied_at:
            continue
        old_date = count.effective_date
        if new_date != old_date:
            count.effective_date = new_date
            audit.record(
                db, user=user, request=request, action="update", entity_type="stock_count",
                entity_id=count.id, entity_label=count.ref_no,
                summary=f"Changed effective date for {count.ref_no}: {old_date or '—'} → {new_date}",
                changes={"effective_date": [str(old_date) if old_date else None, str(new_date)]},
            )
            updated += 1
    if updated:
        db.commit()
    return RedirectResponse(url_with(back, bulk_msg=updated, bulk_error=None), status_code=302)


@router.post("/stock-count/{count_id:int}/scan")
def stock_count_scan(count_id: int, data: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user or not is_floor_staff(user):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    count = db.get(models.StockCount, count_id)
    if not count or count.status != "open":
        return JSONResponse({"ok": False, "error": "This count isn't open anymore."}, status_code=400)

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


@router.post("/stock-count/{count_id:int}/add-shelf")
def stock_count_add_shelf(count_id: int, data: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Bulk-add every product on one shelf as a line (counted_qty starts at
    0, same as a freshly-scanned line) — lets a count be worked shelf by
    shelf instead of hunting the alphabetical product list. Shelves are
    assigned ahead of time (product form or bulk import), not from here."""
    if not user or not is_floor_staff(user):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    count = db.get(models.StockCount, count_id)
    if not count or count.status != "open":
        return JSONResponse({"ok": False, "error": "This count isn't open anymore."}, status_code=400)

    try:
        shelf_id = int(data.get("shelf_id") or 0)
    except (TypeError, ValueError):
        shelf_id = 0
    if not shelf_id:
        return JSONResponse({"ok": False, "error": "Pick a shelf."}, status_code=400)
    products = (
        db.query(models.Product)
        .filter(models.Product.shelf_id == shelf_id, models.Product.is_active.is_(True))
        .all()
    )
    if not products:
        return JSONResponse({"ok": False, "error": "No products are assigned to that shelf yet."}, status_code=404)

    existing_ids = {
        pid for (pid,) in db.query(models.StockCountLine.product_id)
        .filter(models.StockCountLine.stock_count_id == count.id).all()
    }
    added = 0
    for product in products:
        if product.id in existing_ids:
            continue
        db.add(models.StockCountLine(
            stock_count_id=count.id, product_id=product.id, product_name=product.name,
            system_qty=Decimal(str(product.total_qty or 0)), counted_qty=Decimal("0"),
        ))
        added += 1
    db.commit()

    line_rows = (
        db.query(models.StockCountLine)
        .filter(models.StockCountLine.stock_count_id == count.id)
        .order_by(models.StockCountLine.product_name)
        .all()
    )
    return {"ok": True, "added": added, "lines": [_line_dict(l) for l in line_rows]}


@router.post("/stock-count/{count_id:int}/assign-shelf")
def stock_count_assign_shelf(count_id: int, data: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Put every counted product that has no shelf yet onto one shelf.

    The same idea the count-sheet import already offers, but available while
    counting on screen — you're standing at the shelf, you pull in what's
    already assigned to it, you scan the few strays that were never mapped,
    and this is what puts those strays on the map. Like the import, it only
    fills blanks: a product already assigned to another shelf is left alone,
    so this can never quietly move stock somewhere it isn't."""
    if not user or not is_floor_staff(user):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    count = db.get(models.StockCount, count_id)
    if not count or count.status != "open":
        return JSONResponse({"ok": False, "error": "This count isn't open anymore."}, status_code=400)

    try:
        shelf_id = int(data.get("shelf_id") or 0)
    except (TypeError, ValueError):
        shelf_id = 0
    if not shelf_id:
        return JSONResponse({"ok": False, "error": "Pick a shelf."}, status_code=400)
    shelf = db.get(models.Shelf, shelf_id)
    if not shelf:
        return JSONResponse({"ok": False, "error": "That shelf no longer exists."}, status_code=404)

    assigned = []
    for line in count.lines:
        product = line.product
        if product and product.shelf_id is None:
            product.shelf = shelf
            assigned.append(line)
    db.commit()

    line_rows = (
        db.query(models.StockCountLine)
        .filter(models.StockCountLine.stock_count_id == count.id)
        .order_by(models.StockCountLine.product_name)
        .all()
    )
    return {
        "ok": True, "assigned": len(assigned), "shelf": shelf.name,
        "lines": [_line_dict(l) for l in line_rows],
    }


@router.post("/stock-count/{count_id:int}/add-negatives")
def stock_count_add_negatives(count_id: int, back: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Pull every still-negative uncounted product into this count in one go,
    so the shop can go count exactly those instead of hunting them down from
    the warning list by hand."""
    if not user or not is_floor_staff(user):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    count = db.get(models.StockCount, count_id)
    if not count or count.status != "open":
        return JSONResponse({"ok": False, "error": "This count isn't open anymore."}, status_code=400)

    for product in _uncounted_negatives(db, count):
        db.add(models.StockCountLine(
            stock_count_id=count.id, product_id=product.id, product_name=product.name,
            system_qty=Decimal(str(product.total_qty or 0)), counted_qty=Decimal("0"),
        ))
    db.commit()
    return RedirectResponse(_count_url(count.id, back), status_code=302)


@router.get("/stock-count/{count_id:int}/import", response_class=HTMLResponse)
def stock_count_import_form(count_id: int, request: Request, back: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_floor_staff(user):
        return RedirectResponse("/pos", status_code=302)
    count = db.get(models.StockCount, count_id)
    if not count or count.status != "open":
        return RedirectResponse("/stock-count", status_code=302)
    shelves = db.query(models.Shelf).order_by(models.Shelf.name).all()
    return templates.TemplateResponse(
        "stock_count/import.html",
        {"request": request, "app_name": request.app.title, "user": user, "count": count,
         "shelves": shelves, "result": None, "sheet_filters": COUNT_SHEET_FILTERS,
         # the count's page, still carrying where the count was opened from
         "back": safe_back_url(back, ""), "count_url": _count_url(count.id, back)},
    )


@router.post("/stock-count/{count_id:int}/import", response_class=HTMLResponse)
def stock_count_import_upload(
    count_id: int, request: Request, file: UploadFile = File(...), assign_shelf_id: int = Form(0),
    back: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_floor_staff(user):
        return RedirectResponse("/pos", status_code=302)
    count = db.get(models.StockCount, count_id)
    if not count or count.status != "open":
        return RedirectResponse("/stock-count", status_code=302)

    contents = file.file.read()
    rows, error = _parse_upload(
        file.filename, contents,
        header_map=HEADER_MAP_COUNT, fields=FIELDS_COUNT,
        required_field="name", required_label="Product Name",
        keep_extra_columns=True,  # the per-unit columns are named per product
    )
    if error:
        return templates.TemplateResponse(
            "stock_count/import.html",
            {"request": request, "app_name": request.app.title, "user": user, "count": count,
             "result": {"error": error},
             "back": safe_back_url(back, ""), "count_url": _count_url(count.id, back)},
        )

    assign_shelf = db.get(models.Shelf, assign_shelf_id) if assign_shelf_id else None
    classified = _classify_count_import_rows(db, count, list(enumerate(rows, start=2)))
    result = _run_count_import(db, count, classified, assign_shelf=assign_shelf, user=user, request=request)
    result["filename"] = file.filename
    if result["applied"]:
        summary = f"Stock count {count.ref_no}: bulk import from “{file.filename}” set {result['applied']} line(s)"
        if result["shelved"]:
            summary += f", assigned {result['shelved']} product(s) to shelf “{result['assign_shelf_name']}”"
        if result["renamed"]:
            summary += f", renamed {result['renamed']} product(s)"
        audit.record(
            db, user=user, request=request, action="stock_count", entity_type="stock_count",
            entity_id=count.id, entity_label=count.ref_no, summary=summary,
        )
        db.commit()
    return templates.TemplateResponse(
        "stock_count/import.html",
        {"request": request, "app_name": request.app.title, "user": user, "count": count, "result": result,
         "back": safe_back_url(back, ""), "count_url": _count_url(count.id, back)},
    )


# "Which items" choices on the count sheet download: key -> (dropdown label, filename tag).
COUNT_SHEET_FILTERS = {
    "": ("All products", ""),
    "in_count": ("Only items already in this count", "in_count"),
    "linked_negative": ("Linked items + negatives without a link", "linked_and_negatives"),
    "linked": ("Linked items only (whole pack + its Open/Retail)", "linked"),
    "negative_unlinked": ("Negatives without a link", "negatives"),
}


def _filter_count_sheet(products, only: str):
    """Narrow the count sheet to the chosen group. A "linked" item is either
    side of an Open/Retail link — the whole pack (e.g. a sealed Bag) or the
    loose item opened from it — and both sides come out together, pack
    first, so they're counted side by side. Negatives without a link follow,
    by name. Returns (products, filename tag)."""
    if not only or only not in COUNT_SHEET_FILTERS:
        return products, ""
    active_ids = {p.id for p in products}

    def is_linked(p):
        return bool(p.replenish_from_id) or any(o.is_active for o in (p.open_items or []))

    linked, negatives = [], []
    for p in products:
        if is_linked(p):
            linked.append(p)
        elif (p.total_qty or 0) < 0:
            negatives.append(p)

    def family_key(p):
        # Group an Open/Retail item under its whole pack's name.
        src = p.replenish_from if p.replenish_from_id in active_ids else None
        head = src.name if src else p.name
        return (head.lower(), 1 if src else 0, p.name.lower())

    linked.sort(key=family_key)
    picked = {
        "linked_negative": linked + negatives,
        "linked": linked,
        "negative_unlinked": negatives,
    }[only]
    return picked, COUNT_SHEET_FILTERS[only][1]


@router.get("/stock-count/{count_id:int}/import/template")
def stock_count_import_template(count_id: int, shelf_id: int = 0, only: str = "",
                                db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_floor_staff(user):
        return RedirectResponse("/pos", status_code=302)
    count = db.get(models.StockCount, count_id)
    if not count:
        return RedirectResponse("/stock-count", status_code=302)

    existing_by_product = {l.product_id: l for l in count.lines}
    shelf = db.get(models.Shelf, shelf_id) if shelf_id else None
    if only == "in_count":
        # What's already on this count's own sheet — for printing/reviewing
        # just the series you already picked, instead of the full catalog
        # this download normally means.
        product_ids = list(existing_by_product.keys())
        query = db.query(models.Product).filter(models.Product.id.in_(product_ids or [0]), models.Product.is_active.is_(True))
        if shelf:
            query = query.filter(models.Product.shelf_id == shelf.id)
        products = query.order_by(models.Product.name).all()
        only_label = COUNT_SHEET_FILTERS["in_count"][1]
    else:
        query = db.query(models.Product).filter(models.Product.is_active.is_(True))
        if shelf:
            query = query.filter(models.Product.shelf_id == shelf.id)
        products = query.order_by(models.Product.name).all()
        products, only_label = _filter_count_sheet(products, only)

    # One extra column per distinct unit found across the products being
    # exported, so a product sold by the Sack can be counted as "2 Sack"
    # instead of forcing the counter to work out 2 x 25 = 50 kg by hand.
    # A shop with no unit ladders gets the plain 5-column sheet unchanged.
    reserved = set(HEADER_MAP_COUNT)
    unit_headers, seen = [], set()
    for p in products:
        for u in p.units:
            label = (u.name or "").strip()
            key = label.lower()
            if label and key not in seen and key not in reserved:
                seen.add(key)
                unit_headers.append(label)
    unit_headers.sort()
    headers = BASE_HEADERS_COUNT + unit_headers

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = shelf.name[:31] if shelf else "Count Sheet"
    ws.append(headers)
    header_fill = PatternFill("solid", fgColor="1F6FEB")
    unit_fill = PatternFill("solid", fgColor="0F766E")  # per-unit columns read as a distinct group
    for i, cell in enumerate(ws[1]):
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = unit_fill if i >= len(BASE_HEADERS_COUNT) else header_fill

    for p in products:
        line = existing_by_product.get(p.id)
        base_name = p.unit_type.name if p.unit_type else "unit"
        try:
            breakdown = json.loads(line.unit_breakdown) if line and line.unit_breakdown else {}
        except (ValueError, TypeError):
            breakdown = {}
        # Pre-fill from however the count was already entered: a per-unit
        # breakdown goes back into its own columns, a plain number into
        # Counted Qty — so re-downloading mid-count shows the real state.
        if breakdown:
            base_cell = breakdown.get(base_name, "")
        else:
            base_cell = float(line.counted_qty) if line else ""
        row = [p.barcode or "", p.name, (p.shelf.name if p.shelf else ""), "", base_name, float(p.total_qty or 0), base_cell]
        own_units = {(u.name or "").strip().lower() for u in p.units}
        for label in unit_headers:
            key = label.lower()
            row.append(breakdown.get(label, "") if key in own_units else "—")
        ws.append(row)

    widths = [18, 32, 18, 22, 12, 22, 14] + [12] * len(unit_headers)
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "C2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    suffix = (f"_{shelf.name}" if shelf else "") + (f"_{only_label}" if only_label else "")
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{count.ref_no}{suffix}_count_sheet.xlsx"'},
    )


@router.get("/stock-count/{count_id:int}/search")
def stock_count_search(count_id: int, q: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Live typeahead as the scan box is typed into by hand (a barcode
    scanner still just types-and-Enters, unaffected by this) — same
    matching rules as an actual scan, via _find_products."""
    if not user or not is_floor_staff(user):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    matches = _find_products(db, q)
    return {"products": [{"id": p.id, "name": p.name} for p in matches]}


@router.post("/stock-count/{count_id:int}/add-products")
def stock_count_add_products(count_id: int, data: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Add several products at once, checked off a search's results — for
    a series of similarly-named items (sizes, variants) you'd otherwise have
    to search up and click one at a time. Same as a plain scan pick, but
    counted_qty starts at 0 (no assumed "+1") since these were only found,
    not physically counted yet — matches stock_count_add_shelf's own
    starting point."""
    if not user or not is_floor_staff(user):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    count = db.get(models.StockCount, count_id)
    if not count or count.status != "open":
        return JSONResponse({"ok": False, "error": "This count isn't open anymore."}, status_code=400)

    try:
        product_ids = {int(i) for i in (data.get("product_ids") or [])}
    except (TypeError, ValueError):
        product_ids = set()
    if not product_ids:
        return JSONResponse({"ok": False, "error": "Pick at least one product."}, status_code=400)

    existing_ids = {
        pid for (pid,) in db.query(models.StockCountLine.product_id)
        .filter(models.StockCountLine.stock_count_id == count.id).all()
    }
    products = (
        db.query(models.Product)
        .filter(models.Product.id.in_(product_ids), models.Product.is_active.is_(True))
        .all()
    )
    added_lines = []
    for product in products:
        if product.id in existing_ids:
            continue
        line = models.StockCountLine(
            stock_count_id=count.id, product_id=product.id, product_name=product.name,
            system_qty=Decimal(str(product.total_qty or 0)), counted_qty=Decimal("0"),
        )
        db.add(line)
        db.flush()
        added_lines.append(line)
    db.commit()
    return {"ok": True, "lines": [_line_dict(l) for l in added_lines]}


@router.post("/stock-count/{count_id:int}/line/{line_id:int}/set")
def stock_count_set_line(count_id: int, line_id: int, data: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user or not is_floor_staff(user):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    count = db.get(models.StockCount, count_id)
    if not count or count.status != "open":
        return JSONResponse({"ok": False, "error": "This count isn't open anymore."}, status_code=400)
    line = db.get(models.StockCountLine, line_id)
    if not line or line.stock_count_id != count.id:
        return JSONResponse({"ok": False, "error": "Line not found."}, status_code=404)

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


@router.post("/stock-count/{count_id:int}/line/{line_id:int}/delete")
def stock_count_delete_line(count_id: int, line_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Drop a line someone scanned by mistake, or decided not to count after
    all — a plain removal from this count's sheet, not a stock movement, so
    it never touches on-hand or the ledger either way."""
    if not user or not is_floor_staff(user):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    count = db.get(models.StockCount, count_id)
    if not count or count.status != "open":
        return JSONResponse({"ok": False, "error": "This count isn't open anymore."}, status_code=400)
    line = db.get(models.StockCountLine, line_id)
    if not line or line.stock_count_id != count.id:
        return JSONResponse({"ok": False, "error": "Line not found."}, status_code=404)
    db.delete(line)
    db.commit()
    return {"ok": True}


@router.post("/stock-count/{count_id:int}/line/{line_id:int}/set-units")
def stock_count_set_line_units(count_id: int, line_id: int, data: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Per-unit count entry — e.g. '2 FORWARD, 3 Elf, 1 Elf 1/2 physically on
    the shelf' — resolved into the one counted_qty (base units) everything
    else reads, so this is purely a friendlier way to arrive at that number."""
    if not user or not is_floor_staff(user):
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
                          back: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_floor_staff(user):
        return RedirectResponse("/pos", status_code=302)
    count = db.get(models.StockCount, count_id)
    if not count or count.status != "open":
        return RedirectResponse("/stock-count", status_code=302)

    reason_label = ADJUSTMENT_REASON_LABELS.get(reason, reason)
    count_movements = []
    for line in count.lines:
        product = db.get(models.Product, line.product_id, with_for_update=True)
        if not product:
            continue
        scanned_on = line.first_scanned_at.astimezone(MANILA).date() if line.first_scanned_at else None
        stamp = {}
        if count.count_date and scanned_on and scanned_on > count.count_date:
            # Counted on paper and typed in days later: the snapshot taken at
            # scan time already includes sales/deliveries dated after the
            # count, which this count must not cancel out. Compare against
            # what was on hand as of the count date instead — and date the
            # correction on the count date, not the day it was typed in.
            line.system_qty = Decimal(str(product.total_qty or 0)) - qty_dated_after(db, product.id, count.count_date)
            stamp = {"created_at": datetime.combine(count.count_date, time(23, 59, 59), tzinfo=MANILA)}
        variance = Decimal(str(line.counted_qty or 0)) - Decimal(str(line.system_qty or 0))
        if variance == 0:
            continue
        before_beginning = Decimal(str(product.beginning_stock or 0))
        before_stock = Decimal(str(product.stock_qty or 0))
        _apply_stock_count_correction(product, variance)
        unit_cost = Decimal(str(product.cost_price or 0))
        movement = models.StockMovement(
            product_id=product.id, qty_base=variance, reason="stock_count", ref=count.ref_no,
            unit_cost=unit_cost, value=(variance * unit_cost).quantize(Decimal("0.01")), note=reason_label, **stamp,
        )
        db.add(movement)
        count_movements.append(movement)
        changes = {}
        if product.beginning_stock != before_beginning:
            changes["beginning_stock"] = [str(before_beginning), str(product.beginning_stock)]
        if product.stock_qty != before_stock:
            changes["stock_qty"] = [str(before_stock), str(product.stock_qty)]
        audit.record(
            db, user=user, request=request, action="stock_count", entity_type="product",
            entity_id=product.id, entity_label=product.name,
            summary=f"Stock count {count.ref_no}: counted {line.counted_qty:g}, system said {line.system_qty:g}",
            changes=changes,
        )

    count.status = "completed"
    count.completed_by = user.id
    count.completed_at = func.now()
    count.effective_date = _default_effective_date(count.count_date or date.today())

    # The variances' peso value goes to the books on the count date, to the
    # accounts the chosen reason maps to (see inventory_adjustments.REASONS:
    # shortage -> Shrinkage & Losses, surplus -> Inventory Gain; a correction
    # reason -> Inventory Corrections). Never blocks the count — if posting
    # fails, the movements stay unbooked and Reconcile Sales offers a catch-up.
    from . import accounting
    from .inventory_adjustments import REASON_LABELS as ADJ_REASON_LABELS, book_stock_movements
    db.flush()
    try:
        with db.begin_nested():
            book_stock_movements(
                db, count_movements, reason=reason, txn_date=count.count_date or date.today(),
                source_type="stock_count", source_id=count.id, reference_no=count.ref_no,
                description=f"Stock count {count.ref_no} — {ADJ_REASON_LABELS.get(reason, reason_label)}",
                entered_by_id=user.id,
            )
    except accounting.PostingError:
        pass
    db.commit()
    return RedirectResponse(_count_url(count.id, back), status_code=302)


@router.post("/stock-count/{count_id:int}/cancel")
def stock_count_cancel(count_id: int, back: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_floor_staff(user):
        return RedirectResponse("/pos", status_code=302)
    count = db.get(models.StockCount, count_id)
    if count and count.status == "open":
        count.status = "cancelled"
        count.completed_by = user.id
        count.completed_at = func.now()
        db.commit()
    return RedirectResponse(safe_back_url(back, "/stock-count"), status_code=302)


# --- Counted Items: every item across counts, and whether each one goes into
# the Actual Beginning its count sets on the Effective Date. Switchable until
# that date has been applied; each flip is stamped on the line and logged.
ITEMS_PAGE_SIZE = 100
ITEMS_SCOPES = {
    "pending": "Not applied yet",
    "applied": "Already applied",
    "all": "All counts",
}


def _include_editable(count: models.StockCount) -> bool:
    return count.status in ("open", "completed") and not count.effective_applied_at


@router.get("/stock-count/items", response_class=HTMLResponse)
def stock_count_items(
    request: Request, scope: str = "pending", count_id: int = 0, q: str = "", only: str = "",
    page: int = 1, msg: str = "", back: str = "",
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_floor_staff(user):
        return RedirectResponse("/pos", status_code=302)
    scope = scope if scope in ITEMS_SCOPES else "pending"
    SC, SCL = models.StockCount, models.StockCountLine
    base = db.query(SCL).join(SC, SC.id == SCL.stock_count_id).filter(SC.status != "cancelled")
    if scope == "pending":
        base = base.filter(SC.effective_applied_at.is_(None))
    elif scope == "applied":
        base = base.filter(SC.effective_applied_at.isnot(None))
    if count_id:
        base = base.filter(SC.id == count_id)
    if q.strip():
        base = base.filter(multi_word_ilike(SCL.product_name, q))
    excluded_total = base.filter(SCL.include_in_beginning.is_(False)).count()
    rows_q = base
    if only == "variance":
        rows_q = rows_q.filter(SCL.counted_qty != SCL.system_qty)
    elif only == "excluded":
        rows_q = rows_q.filter(SCL.include_in_beginning.is_(False))
    total = rows_q.count()
    pages = max((total + ITEMS_PAGE_SIZE - 1) // ITEMS_PAGE_SIZE, 1)
    page = min(max(page, 1), pages)
    rows = (
        rows_q.order_by(SC.id.desc(), SCL.product_name)
        .offset((page - 1) * ITEMS_PAGE_SIZE).limit(ITEMS_PAGE_SIZE).all()
    )
    items = []
    for l in rows:
        c = l.stock_count
        system, counted = Decimal(str(l.system_qty or 0)), Decimal(str(l.counted_qty or 0))
        items.append({
            "id": l.id, "name": l.product_name, "product_id": l.product_id, "count": c,
            "unit": l.product.unit_type.name if l.product and l.product.unit_type else "",
            "system": system, "counted": counted, "variance": counted - system,
            "include": l.include_in_beginning, "editable": _include_editable(c),
            "changed_at": l.include_changed_at,
            "changed_by": l.include_changer.username if l.include_changer else None,
        })
    counts = db.query(SC).filter(SC.status != "cancelled").order_by(SC.id.desc()).limit(60).all()
    return templates.TemplateResponse(
        "stock_count/items.html",
        {"request": request, "app_name": request.app.title, "user": user,
         "items": items, "counts": counts, "scopes": ITEMS_SCOPES, "scope": scope,
         "count_id": count_id, "q": q, "only": only, "page": page, "pages": pages, "total": total,
         "excluded_total": excluded_total, "can_edit": is_staff(user), "msg": msg,
         # where "← Back" goes: the count it was opened from, else Stock Count
         "back": safe_back_url(back, "/stock-count")},
    )


def _set_include(db: Session, line: models.StockCountLine, include: bool, user, request) -> bool:
    if line.include_in_beginning == include:
        return False
    line.include_in_beginning = include
    line.include_changed_at = func.now()
    line.include_changed_by = user.id
    c = line.stock_count
    audit.record(
        db, user=user, request=request, action="update", entity_type="stock_count",
        entity_id=c.id, entity_label=c.ref_no,
        summary=(f"{c.ref_no}: {'included' if include else 'excluded'} “{line.product_name}” "
                 f"{'in' if include else 'from'} the Actual Beginning set on its effective date"
                 f" ({c.effective_date or 'no date yet'})"),
        changes={"line_id": line.id, "product_id": line.product_id, "include_in_beginning": [not include, include]},
    )
    return True


@router.post("/stock-count/line/{line_id:int}/include")
def stock_count_line_include(line_id: int, request: Request, data: dict,
                             db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Admin/manager only, same as the Effective Date it feeds into."""
    if not user or not is_staff(user):
        return JSONResponse({"ok": False, "error": "Only an admin or manager can change this."}, status_code=403)
    line = db.get(models.StockCountLine, line_id)
    if not line:
        return JSONResponse({"ok": False, "error": "Line not found."}, status_code=404)
    if not _include_editable(line.stock_count):
        return JSONResponse({"ok": False, "error": "This count's effective date has already been applied."}, status_code=400)
    _set_include(db, line, bool(data.get("include")), user, request)
    db.commit()
    db.refresh(line)
    stamp = line.include_changed_at.astimezone(MANILA).strftime("%b %d, %Y %I:%M %p") if line.include_changed_at else ""
    return {"ok": True, "include": line.include_in_beginning, "changed_by": user.username, "changed_at": stamp}


@router.post("/stock-count/items/bulk-include")
def stock_count_items_bulk_include(
    request: Request, line_ids: list[str] = Form([]), include: int = Form(1), back: str = Form(""),
    db: Session = Depends(get_db), user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse("/login", status_code=302)
    back = back if back.startswith("/stock-count/items") else "/stock-count/items"
    if not is_staff(user):
        return RedirectResponse(back, status_code=302)
    changed = 0
    for lid in {int(i) for i in line_ids if i.isdigit()}:
        line = db.get(models.StockCountLine, lid)
        if line and _include_editable(line.stock_count) and _set_include(db, line, bool(include), user, request):
            changed += 1
    if changed:
        db.commit()
    word = "included" if include else "excluded"
    # url_with replaces an earlier notice instead of piling up msg=...&msg=...
    return RedirectResponse(url_with(back, msg=f"{changed} item{'' if changed == 1 else 's'} {word}"), status_code=302)
