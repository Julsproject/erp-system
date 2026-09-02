"""Finds past sales whose stock deduction was likely already reflected in a
completed Stock Count — subtracted twice: once for real when the item left
the shelf, and again when that count (often physically done days earlier,
then typed in later) applied its own correction for the same shortfall.

This is only reconstructable after the fact because a sale's own created_at
gets overwritten the moment it's backdated (see _resolve_txn_datetime in
pos.py) — but the StockMovement rows a sale creates are never touched again
afterward, so their created_at is the one honest record of when the
deduction actually entered the system. A candidate here is real evidence
(movement entered after the count finished, for a date the count's own
count_date already covers), never a guess — see app/pos.py's
_find_backdated_stock_conflicts for the sibling check that runs at sale
entry time, going forward.
"""
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from . import models


def _display_invoice(sale: models.Sale) -> str:
    return f"{sale.receipt_type}{sale.invoice_no}" if sale.receipt_type else sale.invoice_no


def find_double_deduction_candidates(db: Session, product_ids: list) -> dict:
    """For the given product ids, find sale-driven stock deductions that:
    - were entered (real clock, StockMovement.created_at) after a completed
      Stock Count covering that product finished, and
    - belong to a sale whose (possibly backdated) date is on or before that
      count's count_date, and
    - haven't already been corrected (no existing movement points its
      corrects_movement_id back at it).

    Returns {product_id: [{movement_id, qty_base, sale_id, invoice_no,
    sale_date, movement_entered_at, count_ref, count_date}, ...]}, sorted
    oldest-sale-first per product. Product ids with nothing found are
    omitted entirely."""
    if not product_ids:
        return {}

    count_rows = (
        db.query(models.StockCountLine.product_id, models.StockCount)
        .join(models.StockCount, models.StockCount.id == models.StockCountLine.stock_count_id)
        .filter(
            models.StockCountLine.product_id.in_(product_ids),
            models.StockCount.status == "completed",
            models.StockCount.count_date.isnot(None),
        )
        .all()
    )
    covered_by: dict = {}
    for pid, count in count_rows:
        covered_by.setdefault(pid, []).append(count)
    if not covered_by:
        return {}
    covered_ids = list(covered_by.keys())

    already_corrected_ids = {
        row[0] for row in db.query(models.StockMovement.corrects_movement_id)
        .filter(
            models.StockMovement.product_id.in_(covered_ids),
            models.StockMovement.corrects_movement_id.isnot(None),
        ).all()
    }

    movements = (
        db.query(models.StockMovement)
        .filter(
            models.StockMovement.product_id.in_(covered_ids),
            models.StockMovement.reason == "sale",
            models.StockMovement.qty_base < 0,
        )
        .all()
    )
    movements = [m for m in movements if m.id not in already_corrected_ids]
    if not movements:
        return {}

    refs = {m.ref for m in movements if m.ref}
    if not refs:
        return {}
    sales = (
        db.query(models.Sale)
        .filter(
            models.Sale.is_voided.is_(False),
            or_(
                and_(models.Sale.receipt_type.is_(None), models.Sale.invoice_no.in_(refs)),
                func.concat(func.coalesce(models.Sale.receipt_type, ""), models.Sale.invoice_no).in_(refs),
            ),
        )
        .all()
    )
    by_ref = {_display_invoice(s): s for s in sales}

    results: dict = {}
    for m in movements:
        counts_for_product = covered_by.get(m.product_id)
        sale = by_ref.get(m.ref)
        if not counts_for_product or not sale or not m.created_at or not sale.created_at:
            continue
        best = None
        for c in counts_for_product:
            if (
                c.completed_at
                and m.created_at > c.completed_at
                and sale.created_at.date() <= c.count_date
                and (best is None or c.count_date > best.count_date)
            ):
                best = c
        if best is None:
            continue
        results.setdefault(m.product_id, []).append({
            "movement_id": m.id,
            "qty_base": float(m.qty_base),
            "sale_id": sale.id,
            "invoice_no": _display_invoice(sale),
            "sale_date": sale.created_at,
            "movement_entered_at": m.created_at,
            "count_ref": best.ref_no,
            "count_date": best.count_date,
        })

    for pid in results:
        results[pid].sort(key=lambda r: r["sale_date"])
    return results
