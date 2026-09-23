"""Keeping stock movements on their transaction's reference date — the
invoice / DR date — rather than when it happened to be encoded or edited.

The shop encodes a large backlog after the fact, so a sale's or delivery's
date is often corrected later. Its StockMovement rows have to move with it,
and if the move carries it across the product's latest completed Stock Count
its stock effect has to follow the same rule backdated entries already do:
dated on/before the count, the count already reflects it (no effect);
dated after, it counts normally.
"""
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models

MANILA = ZoneInfo("Asia/Manila")


def local_date(ts) -> date | None:
    return ts.astimezone(MANILA).date() if ts else None


def on_date(ts, new_date: date) -> datetime:
    """`ts` moved to `new_date`, keeping its Manila time of day (so a day's
    entries keep their encoding order)."""
    local = (ts or datetime.now(MANILA)).astimezone(MANILA)
    return datetime.combine(new_date, local.timetz())


def latest_count_date(db: Session, product_id: int) -> date | None:
    return (
        db.query(func.max(models.StockCount.count_date))
        .join(models.StockCountLine, models.StockCountLine.stock_count_id == models.StockCount.id)
        .filter(models.StockCountLine.product_id == product_id, models.StockCount.status == "completed")
        .scalar()
    )


def settle_redate(db: Session, product: models.Product, *, old_date: date, new_date: date,
                  current_effect: Decimal, natural_effect: Decimal, ref: str, created_at) -> Decimal:
    """After a transaction's date moved from old_date to new_date: if that
    crossed the product's latest completed count, bring its stock effect on
    this product in line with the rule above — nothing if now dated on/before
    the count, natural_effect (e.g. -qty for a sale) if now after. Returns
    the correction applied (0 when nothing crossed)."""
    from .pos import _apply_stock_count_correction  # pos imports this module

    count_date = latest_count_date(db, product.id)
    if count_date is None or (old_date > count_date) == (new_date > count_date):
        return Decimal("0")
    target = natural_effect if new_date > count_date else Decimal("0")
    diff = target - current_effect
    if diff:
        from .stock_books import book_correction
        cost = Decimal(str(product.cost_price or 0))
        _apply_stock_count_correction(product, diff)
        movement = models.StockMovement(
            product_id=product.id, qty_base=diff, reason="correction", ref=ref,
            unit_cost=cost, value=(diff * cost).quantize(Decimal("0.01")), created_at=created_at,
            note=(f"Date moved to {new_date:%b %d, %Y} — "
                  + ("now after" if new_date > count_date else "now on/before")
                  + f" the {count_date:%b %d} stock count")[:255],
        )
        db.add(movement)
        book_correction(db, movement, count_date=count_date)
    return diff
