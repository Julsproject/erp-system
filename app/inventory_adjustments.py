"""Inventory Adjustments — correct an item's stock and/or its cost in one
document that also posts the peso effect to the books.

Why a document and not just an edit: a bare stock edit used to move the
Stock Card but never the ledger, and a bare cost edit silently re-valued
everything on the shelf with no trace at all. Here every correction has a
reference date, a reason, and one journal entry, and a cancel reverses all
of it (never deletes).

Where the peso difference lands is decided by the reason:

  * correction reasons (encoding / opening balance / cost) → the
    Inventory Corrections equity account: fixing a record isn't a loss, so
    it stays off the P&L. Their qty movements use reason
    "adjustment-correction", which the operational P&L doesn't count.
  * everything else (damage, theft, expired, count shortage, found stock)
    → Inventory Shrinkage & Losses or Inventory Gain, and shows on the P&L
    as it always has (reason "adjustment").
  * a cost change is its own 0-qty "revaluation" movement, valued at the
    on-hand × (new cost − old cost), posted to INV_REVALUATION (the same
    equity account by default; remappable in Accounting Setup).

Within one line the cost change applies first (to what was on hand), then
the qty change at the new cost — so a "found 5 more, and the cost was wrong
too" line values the 5 at the right cost.

Stock follows the adjustment date, same rule as every backdated entry: a qty
change can't be dated on or before the item's latest completed Stock Count
(that count already fixed the stock as of its date).
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

from . import accounting, audit, models
from .database import get_db
from .deps import get_current_user, is_floor_staff, is_staff
from .pos import MANILA, _apply_stock_count_correction
from .stock_count import qty_dated_after
from .stock_dates import latest_count_date, on_date
from .templating import templates

router = APIRouter()

ZERO = Decimal("0")
CENT = Decimal("0.01")
PAGE_SIZE = 30

# (key, label, books) — books is "equity" (off the P&L) or "pnl".
REASONS = [
    ("encoding_correction", "Encoding / opening-balance correction", "equity"),
    ("initial_balance", "Initial balance correction", "equity"),
    ("cost_correction", "Cost correction", "equity"),
    ("count_correction", "Count correction", "pnl"),
    ("damage", "Damage / breakage", "pnl"),
    ("theft", "Theft / loss", "pnl"),
    ("expired", "Expired / spoiled", "pnl"),
    ("found", "Found / extra stock", "pnl"),
    ("other", "Other", "pnl"),
]
REASON_LABELS = {k: label for k, label, _ in REASONS}
REASON_BOOKS = {k: books for k, _, books in REASONS}

SOURCE_LABELS = {"manual": "Adjustment", "product_edit": "Product Edit", "pricing": "Selling Price tab"}

# Movement reasons this module writes.
MV_PNL, MV_CORRECTION, MV_REVAL = "adjustment", "adjustment-correction", "revaluation"


class AdjustmentError(Exception):
    pass


def _dec(value, default="0") -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else default).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise AdjustmentError(f"“{value}” isn't a number.")


def _money(v: Decimal) -> Decimal:
    return Decimal(str(v)).quantize(CENT)


def _today() -> date:
    return datetime.now(MANILA).date()


def books_for(reason: str) -> str:
    return REASON_BOOKS.get(reason, "pnl")


def _stamp(adj: models.InventoryAdjustment) -> datetime:
    """The adjustment's own date, at the current time of day — keeps a
    day's entries in encoding order, same as stock_dates.on_date."""
    now = datetime.now(MANILA)
    return now if adj.adj_date == now.date() else on_date(now, adj.adj_date)


# ---------------------------------------------------------------- computing

def compute_line(db: Session, adj: models.InventoryAdjustment, line: models.InventoryAdjustmentLine,
                 product: models.Product) -> dict:
    """What posting this line would do right now: base-unit qty change,
    cost before/after, and each peso effect. Pure — touches nothing — so the
    draft screen's preview and the real post can never disagree."""
    factor = Decimal(str(line.unit_factor or 1))
    on_hand = Decimal(str(product.total_qty or 0))
    old_cost = Decimal(str(product.cost_price or 0))
    new_cost = Decimal(str(line.new_cost)) if line.new_cost is not None else None
    if new_cost is not None and new_cost == old_cost:
        new_cost = None

    qty_base = ZERO
    on_hand_at_date = None
    if line.qty_input is not None:
        typed = Decimal(str(line.qty_input)) * factor
        if line.mode == "set":
            # The count is what was on hand as of adj_date; everything dated
            # after it still applies on top ("counted + everything dated after").
            on_hand_at_date = on_hand - (qty_dated_after(db, product.id, adj.adj_date)
                                         if adj.adj_date < _today() else ZERO)
            qty_base = typed - on_hand_at_date
        else:
            qty_base = typed

    cost_after = new_cost if new_cost is not None else old_cost
    value_reval = _money(on_hand * (new_cost - old_cost)) if new_cost is not None else ZERO
    value_qty = _money(qty_base * cost_after)
    return {
        "on_hand": on_hand, "on_hand_at_date": on_hand_at_date, "qty_base": qty_base,
        "on_hand_after": on_hand + qty_base, "factor": factor,
        "old_cost": old_cost, "new_cost": new_cost, "cost_after": cost_after,
        "value_reval": value_reval, "value_qty": value_qty, "value_total": value_reval + value_qty,
    }


def _journal_lines(adj: models.InventoryAdjustment, movements: list, *, reversing: bool = False) -> list:
    """Journal lines for a set of adjustment movements. Inventory takes the
    net; each movement's contra account is picked by its reason (and, for a
    P&L movement, whether the ORIGINAL was a loss or a gain — so a cancelled
    loss credits the loss account back instead of landing in Gain)."""
    contra = {}
    net = ZERO
    for m in movements:
        v = Decimal(str(m.value or 0))
        if v == 0:
            continue
        net += v
        if m.reason == MV_REVAL:
            key = "INV_REVALUATION"
        elif m.reason == MV_CORRECTION:
            key = "INV_ADJ_CORRECTION"
        else:
            was_loss = (v > 0) if reversing else (v < 0)
            key = "INV_ADJ_LOSS" if was_loss else "INV_ADJ_GAIN"
        contra[key] = contra.get(key, ZERO) + v
    lines = []
    if net:
        lines.append({"function_key": "INV_ADJ_INVENTORY", "amount": abs(net),
                      "side": "debit" if net > 0 else "credit"})
    for key, v in contra.items():
        if v:
            lines.append({"function_key": key, "amount": abs(v), "side": "credit" if v > 0 else "debit"})
    return lines


def preview_journal(adj: models.InventoryAdjustment, computed: list) -> list:
    """The entry posting would make, as [{account_label, debit, credit}] —
    built through the same _journal_lines the real post uses."""
    class _M:  # a stand-in movement: just reason + value
        def __init__(self, reason, value):
            self.reason, self.value = reason, value
    qty_reason = MV_CORRECTION if books_for(adj.reason) == "equity" else MV_PNL
    fake = []
    for c in computed:
        fake.append(_M(MV_REVAL, c["value_reval"]))
        fake.append(_M(qty_reason, c["value_qty"]))
    return _journal_lines(adj, fake)


# ---------------------------------------------------------------- posting

def post_adjustment(db: Session, adj: models.InventoryAdjustment, *, user, request=None,
                    already_applied: bool = False, books_required: bool = True) -> None:
    """Post a draft: move stock, update cost, write the Stock Card rows and
    one journal entry. Raises AdjustmentError (nothing written) if any line
    can't post. Caller commits.

    already_applied: the stock and cost were already changed by the caller
    (Product Edit saves its fields directly) — record and post the effect
    of that change without applying it again. The line's qty_input/new_cost
    must then describe the change, and the product must (temporarily) hold
    the BEFORE figures — see record_product_edit.

    books_required=False: a failed journal posting (e.g. an unmapped
    account) leaves the stock side recorded instead of refusing."""
    if adj.status != "draft":
        raise AdjustmentError("Only a draft adjustment can be posted.")
    if not adj.lines:
        raise AdjustmentError("Add at least one item before posting.")
    if adj.adj_date > _today():
        raise AdjustmentError("The adjustment date can't be in the future.")

    qty_reason = MV_CORRECTION if books_for(adj.reason) == "equity" else MV_PNL
    reason_label = REASON_LABELS.get(adj.reason, adj.reason)
    stamp = _stamp(adj)
    movements = []
    for line in adj.lines:
        product = db.get(models.Product, line.product_id, with_for_update=True)
        if not product:
            raise AdjustmentError(f"“{line.product_name}” no longer exists.")
        c = compute_line(db, adj, line, product)
        if c["qty_base"] == 0 and c["new_cost"] is None:
            raise AdjustmentError(f"“{line.product_name}”: nothing to change — enter a qty or a new cost different from the current one.")
        if c["new_cost"] is not None and c["new_cost"] <= 0 and not already_applied:
            raise AdjustmentError(f"“{line.product_name}”: the new cost must be more than ₱0.")
        if c["qty_base"] != 0 and not already_applied:
            count_date = latest_count_date(db, product.id)
            if count_date and adj.adj_date <= count_date:
                raise AdjustmentError(
                    f"“{line.product_name}” was counted on {count_date:%b %d, %Y}, and that count already "
                    f"fixed its stock as of that date. Date this adjustment after the count, or use a Stock Count."
                )

        note = (f"{reason_label}: {line.note}" if line.note else reason_label)[:255]
        if c["new_cost"] is not None:
            if not already_applied:
                product.cost_price = c["new_cost"]
            if c["value_reval"]:
                movements.append(models.StockMovement(
                    product_id=product.id, qty_base=ZERO, reason=MV_REVAL, ref=adj.ref_no,
                    unit_cost=c["new_cost"], value=c["value_reval"], created_at=stamp,
                    note=f"Cost ₱{c['old_cost']:,.2f} → ₱{c['new_cost']:,.2f} on {c['on_hand']:g} on hand"[:255],
                    inventory_adjustment_id=adj.id,
                ))
        if c["qty_base"] != 0:
            if not already_applied:
                _apply_stock_count_correction(product, c["qty_base"])
            movements.append(models.StockMovement(
                product_id=product.id, qty_base=c["qty_base"], reason=qty_reason, ref=adj.ref_no,
                unit_cost=c["cost_after"], value=c["value_qty"], created_at=stamp, note=note,
                inventory_adjustment_id=adj.id,
            ))
        line.qty_base = c["qty_base"]
        line.on_hand_before = c["on_hand"]
        line.old_cost = c["old_cost"]
        line.value_qty = c["value_qty"]
        line.value_reval = c["value_reval"]

    for m in movements:
        db.add(m)
    lines = _journal_lines(adj, movements)
    if lines:
        try:
            entry = accounting.post_journal(
                db, txn_date=adj.adj_date, source_type="inventory_adjustment", source_id=adj.id,
                reference_no=adj.ref_no, description=f"Inventory adjustment {adj.ref_no} — {reason_label}",
                lines=lines, entered_by_id=user.id if user else None,
            )
        except accounting.PostingError as e:
            if books_required:
                raise AdjustmentError(f"Couldn't post to the books: {e}")
            entry = None
        adj.journal_entry_id = entry.id if entry else None

    adj.status = "posted"
    adj.posted_by = user.id if user else None
    adj.posted_at = func.now()
    total = sum((Decimal(str(m.value or 0)) for m in movements), ZERO)
    audit.record(
        db, user=user, request=request, action="adjust_stock", entity_type="inventory_adjustment",
        entity_id=adj.id, entity_label=adj.ref_no,
        summary=f"Posted {adj.ref_no} ({reason_label}) — {len(adj.lines)} item(s), ₱{total:,.2f} inventory value",
    )


def cancel_adjustment(db: Session, adj: models.InventoryAdjustment, *, user, reason: str, request=None) -> None:
    """Reverse a posted adjustment: stock back, cost back, and an opposite
    journal entry, all dated on the adjustment's own date so every period
    report nets to zero. Refuses (AdjustmentError) where reversing would be
    wrong rather than undo: a Stock Count has since re-counted the item, or
    the item's cost has moved on since (e.g. a purchase re-averaged it)."""
    if adj.status != "posted":
        raise AdjustmentError("Only a posted adjustment can be cancelled.")
    originals = (
        db.query(models.StockMovement)
        .filter(models.StockMovement.inventory_adjustment_id == adj.id)
        .order_by(models.StockMovement.id)
        .all()
    )
    reversals = []
    stamp = originals[0].created_at if originals else _stamp(adj)
    by_product = {}
    for m in originals:
        by_product.setdefault(m.product_id, []).append(m)
    for line in adj.lines:
        product = db.get(models.Product, line.product_id, with_for_update=True)
        mvs = by_product.get(line.product_id, [])
        qty_mv = next((m for m in mvs if m.reason in (MV_PNL, MV_CORRECTION)), None)
        reval_mv = next((m for m in mvs if m.reason == MV_REVAL), None)
        if qty_mv is not None:
            count_date = latest_count_date(db, product.id)
            if count_date and count_date >= adj.adj_date:
                raise AdjustmentError(
                    f"“{line.product_name}” was counted on {count_date:%b %d, %Y}, after this adjustment — "
                    f"that count already includes it. Cancelling would change stock twice."
                )
        changed_cost = line.old_cost is not None and line.new_cost is not None and line.old_cost != line.new_cost
        if changed_cost and Decimal(str(product.cost_price or 0)) != Decimal(str(line.new_cost)):
            raise AdjustmentError(
                f"“{line.product_name}”'s cost has changed since (now ₱{product.cost_price:,.2f}), so it can't "
                f"simply go back to ₱{line.old_cost:,.2f}. Post a new cost adjustment instead."
            )
        note = f"Cancelled {adj.ref_no}"
        if qty_mv is not None:
            q = Decimal(str(qty_mv.qty_base))
            _apply_stock_count_correction(product, -q)
            reversals.append(models.StockMovement(
                product_id=product.id, qty_base=-q, reason=qty_mv.reason, ref=adj.ref_no,
                unit_cost=qty_mv.unit_cost, value=-Decimal(str(qty_mv.value or 0)), created_at=stamp,
                note=note, inventory_adjustment_id=adj.id,
            ))
        if changed_cost:
            old_cost = Decimal(str(line.old_cost))
            on_hand = Decimal(str(product.total_qty or 0))
            value = _money(on_hand * (old_cost - Decimal(str(product.cost_price or 0))))
            product.cost_price = old_cost
            if value or reval_mv is not None:
                reversals.append(models.StockMovement(
                    product_id=product.id, qty_base=ZERO, reason=MV_REVAL, ref=adj.ref_no,
                    unit_cost=old_cost, value=value, created_at=stamp,
                    note=f"{note} — cost back to ₱{old_cost:,.2f}", inventory_adjustment_id=adj.id,
                ))

    for m in reversals:
        db.add(m)
    lines = _journal_lines(adj, reversals, reversing=True)
    if lines:
        try:
            entry = accounting.post_journal(
                db, txn_date=adj.adj_date, source_type="inventory_adjustment", source_id=adj.id,
                reference_no=adj.ref_no, description=f"Cancelled inventory adjustment {adj.ref_no}",
                lines=lines, entered_by_id=user.id if user else None,
            )
        except accounting.PostingError as e:
            raise AdjustmentError(f"Couldn't post the reversal to the books: {e}")
        adj.cancel_journal_entry_id = entry.id
    adj.status = "cancelled"
    adj.cancelled_by = user.id if user else None
    adj.cancelled_at = func.now()
    adj.cancel_reason = (reason or "").strip()[:255] or None
    audit.record(
        db, user=user, request=request, action="cancel", entity_type="inventory_adjustment",
        entity_id=adj.id, entity_label=adj.ref_no,
        summary=f"Cancelled {adj.ref_no}" + (f": {adj.cancel_reason}" if adj.cancel_reason else ""),
    )


class _ValueRow:
    """A stand-in movement for _journal_lines: just a reason and a value."""
    def __init__(self, reason, value):
        self.reason, self.value = reason, value


def book_stock_movements(db: Session, movements: list, *, reason: str, txn_date: date, source_type: str,
                         source_id, reference_no: str, description: str, entered_by_id: int = None):
    """Post the peso value of stock movements that were written without a
    journal entry (a completed Stock Count, a bulk import) — the same
    accounts an Inventory Adjustment with this `reason` would use — and mark
    each one booked (journal_entry_id) so nothing posts it twice. Returns the
    entry, or None when there's no peso effect. Raises accounting.PostingError."""
    todo = [m for m in movements if m.journal_entry_id is None and m.value is not None and Decimal(str(m.value)) != 0]
    kind = MV_CORRECTION if books_for(reason) == "equity" else MV_PNL
    lines = _journal_lines(None, [_ValueRow(kind, m.value) for m in todo])
    if not lines:
        return None
    entry = accounting.post_journal(
        db, txn_date=txn_date, source_type=source_type, source_id=source_id, reference_no=reference_no,
        description=description, lines=lines, entered_by_id=entered_by_id,
    )
    for m in todo:
        m.journal_entry_id = entry.id
    return entry


REASON_KEY_BY_LABEL = {label: key for key, label, _ in REASONS}


def unbooked_stock_movements(db: Session) -> list:
    """Count / bulk-import movements with a peso value that never reached the
    books — normally none; a posting that failed (e.g. an unmapped account)
    leaves them here for Reconcile Sales' catch-up."""
    return (
        db.query(models.StockMovement)
        .filter(models.StockMovement.reason.in_(("stock_count", MV_PNL, MV_CORRECTION)),
                models.StockMovement.inventory_adjustment_id.is_(None),
                models.StockMovement.journal_entry_id.is_(None),
                models.StockMovement.value.isnot(None), models.StockMovement.value != 0)
        .order_by(models.StockMovement.created_at)
        .all()
    )


def book_unbooked_stock_movements(db: Session, *, entered_by_id: int = None):
    """Catch-up for unbooked_stock_movements: one entry per stock count (or
    per day of other edits), dated the movements' own date. A count's reason
    is read back from the note its movements carry; anything else is a data
    load (bulk import) and goes to Inventory Corrections."""
    groups = {}
    for m in unbooked_stock_movements(db):
        d = m.created_at.astimezone(MANILA).date()
        key = (d, m.ref if m.reason == "stock_count" and m.ref else "")
        groups.setdefault(key, []).append(m)
    posted = []
    for (d, ref), items in sorted(groups.items()):
        if ref:
            count = db.query(models.StockCount).filter(models.StockCount.ref_no == ref).first()
            reason = REASON_KEY_BY_LABEL.get((items[0].note or "").strip(), "count_correction")
            entry = book_stock_movements(
                db, items, reason=reason, txn_date=d, source_type="stock_count",
                source_id=count.id if count else None, reference_no=ref,
                description=f"Stock count {ref} — {REASON_LABELS.get(reason, reason)}", entered_by_id=entered_by_id,
            )
        else:
            entry = book_stock_movements(
                db, items, reason="encoding_correction", txn_date=d, source_type="inventory_history",
                source_id=None, reference_no=None, description=f"Stock edits {d:%b %d, %Y} (bulk import)",
                entered_by_id=entered_by_id,
            )
        if entry:
            posted.append(entry)
    return posted


def _new_adjustment(db: Session, *, adj_date: date, reason: str, notes: str = None,
                    source: str = "manual", user=None) -> models.InventoryAdjustment:
    adj = models.InventoryAdjustment(
        adj_date=adj_date, reason=reason, notes=notes, source=source,
        created_by=user.id if user else None,
    )
    db.add(adj)
    db.flush()
    adj.ref_no = f"ADJ-{adj.id:06d}"
    return adj


def record_product_edit(db: Session, product: models.Product, *, old_total: Decimal, old_cost: Decimal,
                        reason: str, note: str = None, source: str = "product_edit", user=None, request=None):
    """Product Edit / the Selling Price tab changed stock and/or cost
    directly: turn that change into a posted adjustment (dated today) so it
    lands on the Stock Card AND in the books. Nothing to do if neither
    changed. A books posting failure never blocks the edit itself — same
    rule as sales and purchases — the stock side is still recorded."""
    new_total = Decimal(str(product.total_qty or 0))
    new_cost = Decimal(str(product.cost_price or 0))
    delta = new_total - old_total
    cost_changed = new_cost != old_cost
    if delta == 0 and not cost_changed:
        return None
    if reason not in REASON_LABELS:
        reason = "cost_correction" if delta == 0 else "other"

    adj = _new_adjustment(db, adj_date=_today(), reason=reason, notes=note, source=source, user=user)
    line = models.InventoryAdjustmentLine(
        adjustment_id=adj.id, product_id=product.id, product_name=product.name,
        unit_name=product.unit_type.name if product.unit_type else None, unit_factor=Decimal("1"),
        mode="delta", qty_input=delta if delta else None,
        new_cost=new_cost if cost_changed else None, note=note,
    )
    adj.lines.append(line)
    # compute_line reads the product's CURRENT figures as "before"; they've
    # already been saved, so put the old ones back just while posting.
    saved_cost, saved_beginning, saved_stock = product.cost_price, product.beginning_stock, product.stock_qty
    product.cost_price = old_cost
    product.stock_qty = Decimal(str(saved_stock or 0)) - delta  # total back to old_total
    try:
        post_adjustment(db, adj, user=user, request=request, already_applied=True, books_required=False)
    finally:
        product.cost_price, product.beginning_stock, product.stock_qty = saved_cost, saved_beginning, saved_stock
    return adj


# ---------------------------------------------------------------- screens

def _render(request, template, user, **ctx):
    return templates.TemplateResponse(template, {
        "request": request, "app_name": request.app.title, "user": user,
        "reasons": REASONS, "reason_labels": REASON_LABELS, "reason_books": REASON_BOOKS,
        "source_labels": SOURCE_LABELS, **ctx,
    })


def _parse_date(s: str):
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


@router.get("/inventory-adjustments", response_class=HTMLResponse)
def adjustment_list(request: Request, status: str = "", q: str = "", page: int = 1,
                    db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_floor_staff(user):
        return RedirectResponse("/pos", status_code=302)
    query = db.query(models.InventoryAdjustment).options(selectinload(models.InventoryAdjustment.lines))
    if status in ("draft", "posted", "cancelled"):
        query = query.filter(models.InventoryAdjustment.status == status)
    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            models.InventoryAdjustment.ref_no.ilike(like),
            models.InventoryAdjustment.notes.ilike(like),
            models.InventoryAdjustment.lines.any(models.InventoryAdjustmentLine.product_name.ilike(like)),
        ))
    total = query.count()
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(max(page, 1), pages)
    rows = (query.order_by(models.InventoryAdjustment.adj_date.desc(), models.InventoryAdjustment.id.desc())
            .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all())
    counts = dict(db.query(models.InventoryAdjustment.status, func.count()).group_by(models.InventoryAdjustment.status).all())
    return _render(request, "inventory_adjustments/list.html", user, rows=rows, status=status, q=q,
                   page=page, pages=pages, total=total, counts=counts, today=_today())


@router.post("/inventory-adjustments/new")
def adjustment_create(request: Request, adj_date: str = Form(""), reason: str = Form(""), notes: str = Form(""),
                      product_id: int = Form(0),
                      db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_floor_staff(user):
        return RedirectResponse("/pos", status_code=302)
    d = _parse_date(adj_date) or _today()
    if reason not in REASON_LABELS:
        reason = "encoding_correction"
    adj = _new_adjustment(db, adj_date=min(d, _today()), reason=reason, notes=notes.strip()[:255] or None, user=user)
    db.commit()
    suffix = f"?add={product_id}" if product_id else ""
    return RedirectResponse(f"/inventory-adjustments/{adj.id}{suffix}", status_code=302)


def _product_payload(p: models.Product) -> dict:
    base = p.unit_type.name if p.unit_type else "Unit"
    units = [{"name": base, "factor": 1.0}] + [
        {"name": u.name, "factor": float(u.factor_to_base or 1)} for u in p.units if (u.factor_to_base or 0) > 0
    ]
    return {"id": p.id, "name": p.name, "base_unit": base, "units": units,
            "cost_price": float(p.cost_price or 0), "on_hand": float(p.total_qty or 0)}


@router.get("/inventory-adjustments/product-search")
def adjustment_product_search(q: str = "", id: int = 0, db: Session = Depends(get_db), user=Depends(get_current_user)):
    from .search_utils import multi_word_ilike
    if not user or not is_floor_staff(user):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    query = db.query(models.Product).options(selectinload(models.Product.units)).filter(models.Product.is_active.is_(True))
    if id:
        query = query.filter(models.Product.id == id)
    elif (q or "").strip():
        query = query.filter(multi_word_ilike(models.Product.name, q.strip()))
    else:
        return {"products": []}
    return {"products": [_product_payload(p) for p in query.order_by(models.Product.name).limit(25).all()]}


@router.get("/inventory-adjustments/{adj_id:int}", response_class=HTMLResponse)
def adjustment_view(adj_id: int, request: Request, error: str = "", add: int = 0,
                    db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_floor_staff(user):
        return RedirectResponse("/pos", status_code=302)
    adj = db.get(models.InventoryAdjustment, adj_id)
    if not adj:
        return RedirectResponse("/inventory-adjustments", status_code=302)

    rows, computed = [], []
    for line in adj.lines:
        product = db.get(models.Product, line.product_id)
        if adj.status == "draft":
            c = compute_line(db, adj, line, product)
            computed.append(c)
        else:
            c = {
                "on_hand": Decimal(str(line.on_hand_before or 0)), "qty_base": Decimal(str(line.qty_base or 0)),
                "old_cost": Decimal(str(line.old_cost or 0)),
                "new_cost": Decimal(str(line.new_cost)) if line.new_cost is not None and line.new_cost != line.old_cost else None,
                "value_qty": Decimal(str(line.value_qty or 0)), "value_reval": Decimal(str(line.value_reval or 0)),
                "factor": Decimal(str(line.unit_factor or 1)), "on_hand_at_date": None,
            }
            c["on_hand_after"] = c["on_hand"] + c["qty_base"]
            c["value_total"] = c["value_qty"] + c["value_reval"]
        rows.append({"line": line, "product": product, "c": c})

    je_preview = []
    if adj.status == "draft" and computed:
        maps = {m.function_key: m.account for m in db.query(models.AccountMapping).all()}
        for ln in preview_journal(adj, computed):
            acct = maps.get(ln["function_key"])
            je_preview.append({
                "account": f"{acct.code} {acct.name}" if acct else ln["function_key"],
                "debit": ln["amount"] if ln["side"] == "debit" else None,
                "credit": ln["amount"] if ln["side"] == "credit" else None,
            })
    total_value = sum((r["c"]["value_total"] for r in rows), ZERO)
    add_product = None
    if add and adj.status == "draft":
        p = db.get(models.Product, add)
        add_product = _product_payload(p) if p else None
    return _render(request, "inventory_adjustments/view.html", user, adj=adj, rows=rows, je_preview=je_preview,
                   total_value=total_value, error=error, today=_today(), add_product=add_product,
                   can_post=is_staff(user))


def _draft_or_redirect(db, adj_id):
    adj = db.get(models.InventoryAdjustment, adj_id)
    if not adj or adj.status != "draft":
        return None, RedirectResponse(f"/inventory-adjustments/{adj_id}", status_code=302)
    return adj, None


def _err(adj_id, msg):
    from urllib.parse import quote
    return RedirectResponse(f"/inventory-adjustments/{adj_id}?error={quote(msg)}", status_code=302)


@router.post("/inventory-adjustments/{adj_id:int}/header")
def adjustment_header(adj_id: int, adj_date: str = Form(""), reason: str = Form(""), notes: str = Form(""),
                      db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user or not is_floor_staff(user):
        return RedirectResponse("/login", status_code=302)
    adj, redirect = _draft_or_redirect(db, adj_id)
    if redirect:
        return redirect
    d = _parse_date(adj_date)
    if not d:
        return _err(adj_id, "Enter a valid date.")
    if d > _today():
        return _err(adj_id, "The adjustment date can't be in the future.")
    adj.adj_date = d
    if reason in REASON_LABELS:
        adj.reason = reason
    adj.notes = notes.strip()[:255] or None
    db.commit()
    return RedirectResponse(f"/inventory-adjustments/{adj_id}", status_code=302)


@router.post("/inventory-adjustments/{adj_id:int}/lines")
def adjustment_add_line(adj_id: int, product_id: int = Form(...), unit_name: str = Form(""),
                        mode: str = Form("delta"), qty: str = Form(""), new_cost: str = Form(""), note: str = Form(""),
                        db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user or not is_floor_staff(user):
        return RedirectResponse("/login", status_code=302)
    adj, redirect = _draft_or_redirect(db, adj_id)
    if redirect:
        return redirect
    product = db.get(models.Product, product_id)
    if not product:
        return _err(adj_id, "Pick an item first.")
    if any(l.product_id == product.id for l in adj.lines):
        return _err(adj_id, f"“{product.name}” is already on this adjustment — remove that line first to change it.")
    base = product.unit_type.name if product.unit_type else "Unit"
    factor = Decimal("1")
    if unit_name and unit_name != base:
        unit = next((u for u in product.units if u.name == unit_name), None)
        if not unit or not unit.factor_to_base or unit.factor_to_base <= 0:
            return _err(adj_id, f"“{unit_name}” isn't a unit of {product.name}.")
        factor = Decimal(str(unit.factor_to_base))
    else:
        unit_name = base
    try:
        qty_val = _dec(qty) if (qty or "").strip() else None
        cost_val = _dec(new_cost) if (new_cost or "").strip() else None
    except AdjustmentError as e:
        return _err(adj_id, str(e))
    mode = "set" if mode == "set" else "delta"
    if qty_val is not None and mode == "set" and qty_val < 0:
        return _err(adj_id, "A counted quantity can't be negative.")
    if qty_val is not None and mode == "delta" and qty_val == 0:
        qty_val = None
    if cost_val is not None:
        if cost_val <= 0:
            return _err(adj_id, "The new cost must be more than ₱0.")
        cost_val = (cost_val / factor).quantize(CENT)  # typed per the chosen unit; stored per base unit
    if qty_val is None and cost_val is None:
        return _err(adj_id, "Enter a quantity, a new cost, or both.")
    adj.lines.append(models.InventoryAdjustmentLine(
        product_id=product.id, product_name=product.name, unit_name=unit_name, unit_factor=factor,
        mode=mode, qty_input=qty_val, new_cost=cost_val, note=note.strip()[:255] or None,
    ))
    db.commit()
    return RedirectResponse(f"/inventory-adjustments/{adj_id}", status_code=302)


@router.post("/inventory-adjustments/{adj_id:int}/lines/{line_id:int}/delete")
def adjustment_delete_line(adj_id: int, line_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user or not is_floor_staff(user):
        return RedirectResponse("/login", status_code=302)
    adj, redirect = _draft_or_redirect(db, adj_id)
    if redirect:
        return redirect
    line = next((l for l in adj.lines if l.id == line_id), None)
    if line:
        adj.lines.remove(line)
        db.commit()
    return RedirectResponse(f"/inventory-adjustments/{adj_id}", status_code=302)


@router.post("/inventory-adjustments/{adj_id:int}/delete")
def adjustment_delete_draft(adj_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """A draft never touched stock or the books, so it can simply go."""
    if not user or not is_floor_staff(user):
        return RedirectResponse("/login", status_code=302)
    adj, redirect = _draft_or_redirect(db, adj_id)
    if redirect:
        return redirect
    db.delete(adj)
    db.commit()
    return RedirectResponse("/inventory-adjustments", status_code=302)


@router.post("/inventory-adjustments/{adj_id:int}/post")
def adjustment_post(adj_id: int, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return _err(adj_id, "Only a manager or admin can post an adjustment.")
    adj, redirect = _draft_or_redirect(db, adj_id)
    if redirect:
        return redirect
    try:
        post_adjustment(db, adj, user=user, request=request)
    except AdjustmentError as e:
        db.rollback()
        return _err(adj_id, str(e))
    db.commit()
    return RedirectResponse(f"/inventory-adjustments/{adj_id}", status_code=302)


@router.post("/inventory-adjustments/{adj_id:int}/cancel")
def adjustment_cancel(adj_id: int, request: Request, reason: str = Form(""),
                      db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not is_staff(user):
        return _err(adj_id, "Only a manager or admin can cancel an adjustment.")
    adj = db.get(models.InventoryAdjustment, adj_id)
    if not adj:
        return RedirectResponse("/inventory-adjustments", status_code=302)
    if not (reason or "").strip():
        return _err(adj_id, "Give a reason for cancelling.")
    try:
        cancel_adjustment(db, adj, user=user, reason=reason, request=request)
    except AdjustmentError as e:
        db.rollback()
        return _err(adj_id, str(e))
    db.commit()
    return RedirectResponse(f"/inventory-adjustments/{adj_id}", status_code=302)
