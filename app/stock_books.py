"""Keeping the ledger's Inventory account in step with the Stock Card.

The books post transactions (a sale's cost, a delivery's invoice); the
Stock Card moves stock. They disagree wherever one moves without the other:

  * A sale or delivery dated on/before a completed Stock Count gets no stock
    effect — the count already reflects it. The count's difference was
    booked when it completed, so the transaction's cost must not go through
    Inventory a second time: that part goes against the account the count
    used instead (sale_count_covered / purchase_count_covered).
  * Re-dating a transaction across a count writes a "correction" movement
    that changes stock with no transaction of its own — book_correction.
  * A cost that changes without the goods changing — a delivery re-averaging
    an item already sold into negative stock, a pack opened into an
    Open/Retail item resetting its cost — re-values what's on hand;
    book_cost_variance posts that to cost of sales.
"""
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models

MANILA = ZoneInfo("Asia/Manila")
ZERO = Decimal("0")
CENT = Decimal("0.01")


def _local(ts) -> date:
    return ts.astimezone(MANILA).date() if ts else datetime.now(MANILA).date()


def _mapping_account_id(db: Session, function_key: str) -> int:
    from .accounting import _resolve_mapping
    return _resolve_mapping(db, function_key).id


def count_contra_account_id(db: Session, product_id: int, on_date: date) -> int:
    """The account the Stock Count that absorbed a transaction dated
    `on_date` booked this product's difference to — the first completed count
    of the product dated on/after it. Falls back to Inventory Corrections."""
    count = (
        db.query(models.StockCount)
        .join(models.StockCountLine, models.StockCountLine.stock_count_id == models.StockCount.id)
        .filter(models.StockCountLine.product_id == product_id, models.StockCount.status == "completed",
                models.StockCount.count_date >= on_date)
        .order_by(models.StockCount.count_date, models.StockCount.id)
        .first()
    )
    if count:
        mv = (
            db.query(models.StockMovement)
            .filter(models.StockMovement.reason == "stock_count", models.StockMovement.ref == count.ref_no,
                    models.StockMovement.product_id == product_id, models.StockMovement.journal_entry_id.isnot(None))
            .first()
        )
        if mv:
            inv_id = _mapping_account_id(db, "INV_ADJ_INVENTORY")
            others = {ln.account_id for ln in db.get(models.JournalEntry, mv.journal_entry_id).lines
                      if ln.account_id != inv_id}
            if len(others) == 1:
                return others.pop()
    return _mapping_account_id(db, "INV_ADJ_CORRECTION")


def sale_count_covered(db: Session, sale: models.Sale) -> dict:
    """{account_id: cost} of this sale's goods whose stock a Stock Count
    already took out (their "sale" movement moved less than was sold). A
    product with no movement carrying this invoice # at all predates
    invoice refs on movements and is assumed fully moved."""
    if sale.txn_type != "sale":
        return {}
    db.flush()  # the session doesn't autoflush; the caller's movements must be visible
    ref = f"{sale.receipt_type}{sale.invoice_no}" if sale.receipt_type else sale.invoice_no
    sale_date = _local(sale.created_at)
    cost_by_pid = {}
    for ln in sale.lines:
        if ln.product_id:
            cost_by_pid[ln.product_id] = cost_by_pid.get(ln.product_id, ZERO) + (
                Decimal(str(ln.qty or 0)) * Decimal(str(ln.unit_factor or 1)) * Decimal(str(ln.unit_cost or 0)))
    if not cost_by_pid:
        return {}
    moved = {}
    for m in (db.query(models.StockMovement)
              .filter(models.StockMovement.ref == ref, models.StockMovement.product_id.in_(cost_by_pid),
                      models.StockMovement.reason.in_(("sale", "sale-edit-reverse"))).all()):
        if _local(m.created_at) != sale_date:
            continue  # another sale sharing this invoice # on a different day
        moved[m.product_id] = moved.get(m.product_id, ZERO) - Decimal(str(m.value or 0))
    out = {}
    for pid, cost in cost_by_pid.items():
        if pid not in moved:
            continue
        covered = (cost - moved[pid]).quantize(CENT)
        if covered:
            acct = count_contra_account_id(db, pid, sale_date)
            out[acct] = out.get(acct, ZERO) + covered
    return out


def purchase_count_covered(db: Session, purchase: models.Purchase) -> dict:
    """{account_id: net cost} of this delivery (or return) whose stock a
    Stock Count already accounted for — the part its movements didn't move."""
    if not purchase.ref_no or not purchase.lines:
        return {}
    db.flush()  # the session doesn't autoflush; movements just re-referenced must be visible
    total = Decimal(str(purchase.total or 0))
    if total <= 0:
        return {}
    net_ratio = (total - Decimal(str(purchase.vat_amount or 0))) / total
    sign = Decimal("-1") if purchase.txn_type == "return" else Decimal("1")
    moved = {}
    for m in (db.query(models.StockMovement)
              .filter(models.StockMovement.ref == purchase.ref_no,
                      models.StockMovement.reason.in_(("purchase", "purchase-edit-reverse", "purchase-return"))).all()):
        moved[m.product_id] = moved.get(m.product_id, ZERO) + Decimal(str(m.qty_base or 0))
    expected, net_by_pid = {}, {}
    for ln in purchase.lines:
        if not ln.product_id:
            continue
        expected[ln.product_id] = expected.get(ln.product_id, ZERO) + sign * Decimal(str(ln.qty or 0)) * Decimal(str(ln.unit_factor or 1))
        net_by_pid[ln.product_id] = net_by_pid.get(ln.product_id, ZERO) + Decimal(str(ln.line_total or 0)) * net_ratio
    out = {}
    d = _local(purchase.created_at)
    for pid, exp in expected.items():
        if pid not in moved or not exp:
            continue
        fraction = 1 - moved[pid] / exp
        covered = (net_by_pid[pid] * fraction).quantize(CENT)
        if covered:
            acct = count_contra_account_id(db, pid, d)
            out[acct] = out.get(acct, ZERO) + covered
    return out


def _post(db: Session, *, amount: Decimal, contra_account_id: int, txn_date: date, source_type: str,
          source_id, reference_no, description: str, entered_by_id=None):
    """Dr Inventory / Cr contra for a positive amount (stock value up), the
    other way round for a negative one."""
    from .accounting import post_journal
    amount = Decimal(str(amount)).quantize(CENT)
    if not amount:
        return None
    up = amount > 0
    return post_journal(
        db, txn_date=txn_date, source_type=source_type, source_id=source_id, reference_no=reference_no,
        description=description, entered_by_id=entered_by_id,
        lines=[{"function_key": "INV_ADJ_INVENTORY", "amount": abs(amount), "side": "debit" if up else "credit"},
               {"_account_id": contra_account_id, "amount": abs(amount), "side": "credit" if up else "debit"}],
    )


def book_correction(db: Session, movement: models.StockMovement, *, count_date: date, entered_by_id=None):
    """A re-date moved stock across a count (stock_dates.settle_redate):
    book the stock value change against that count's account. Never blocks
    the re-date — an unmapped account just leaves it for the true-up."""
    from .accounting import PostingError
    try:
        with db.begin_nested():
            db.flush()
            entry = _post(
                db, amount=Decimal(str(movement.value or 0)),
                contra_account_id=count_contra_account_id(db, movement.product_id, count_date),
                txn_date=_local(movement.created_at), source_type="stock_correction", source_id=movement.id,
                reference_no=movement.ref, description=f"Stock re-dated across the {count_date:%b %d} count — {movement.ref}",
                entered_by_id=entered_by_id,
            )
            if entry:
                movement.journal_entry_id = entry.id
    except PostingError:
        pass


def book_cost_variance(db: Session, product: models.Product, *, value_before: Decimal, value_added: Decimal,
                       ref: str, note: str, stamp=None, entered_by_id=None):
    """After a cost change that came with (or without) goods coming in:
    whatever the shelf value moved beyond value_added is a re-valuation of
    goods already there — or already sold into negative stock. Recorded as a
    0-qty "revaluation" Stock Card row and posted Inventory vs
    INV_COST_VARIANCE (cost of sales), so the ledger follows the shelf."""
    from .accounting import PostingError
    if not isinstance(stamp, datetime):
        stamp = None  # e.g. a not-backdated delivery stamps with the SQL now(); the column default does the same
    actual = Decimal(str(product.total_qty or 0)) * Decimal(str(product.cost_price or 0))
    variance = (actual - Decimal(str(value_before)) - Decimal(str(value_added))).quantize(CENT)
    if not variance:
        return None
    mv = models.StockMovement(
        product_id=product.id, qty_base=ZERO, reason="revaluation", ref=ref,
        unit_cost=product.cost_price, value=variance, note=note[:255],
        **({"created_at": stamp} if stamp is not None else {}),
    )
    db.add(mv)
    try:
        with db.begin_nested():
            db.flush()
            entry = _post(
                db, amount=variance, contra_account_id=_mapping_account_id(db, "INV_COST_VARIANCE"),
                txn_date=_local(stamp) if stamp is not None else datetime.now(MANILA).date(),
                source_type="cost_variance", source_id=mv.id, reference_no=ref,
                description=f"Cost re-averaged — {product.name}"[:255], entered_by_id=entered_by_id,
            )
            if entry:
                mv.journal_entry_id = entry.id
    except PostingError:
        pass
    return mv


def shelf_value(db: Session) -> Decimal:
    return Decimal(str(db.query(func.coalesce(func.sum(
        (models.Product.beginning_stock + models.Product.stock_qty) * models.Product.cost_price), 0)).scalar())).quantize(CENT)


def ledger_inventory(db: Session) -> Decimal:
    inv_id = _mapping_account_id(db, "INV_ADJ_INVENTORY")
    d, c = (db.query(func.coalesce(func.sum(models.JournalLine.debit), 0), func.coalesce(func.sum(models.JournalLine.credit), 0))
            .join(models.JournalEntry, models.JournalLine.entry_id == models.JournalEntry.id)
            .filter(models.JournalLine.account_id == inv_id, models.JournalEntry.status != "draft").one())
    return (Decimal(str(d)) - Decimal(str(c))).quantize(CENT)


def post_true_up(db: Session, *, entered_by_id=None, description: str = None):
    """Book whatever difference is left between the ledger's Inventory and
    stock on hand x cost (all items, archived included) to INV_TRUE_UP."""
    diff = shelf_value(db) - ledger_inventory(db)
    if not diff:
        return None, diff
    entry = _post(
        db, amount=diff, contra_account_id=_mapping_account_id(db, "INV_TRUE_UP"),
        txn_date=datetime.now(MANILA).date(), source_type="inventory_true_up", source_id=None,
        reference_no="INV-TRUE-UP", entered_by_id=entered_by_id,
        description=description or "Inventory true-up — ledger brought to stock on hand x cost",
    )
    return entry, diff
