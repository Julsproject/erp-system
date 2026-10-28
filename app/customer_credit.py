"""A customer's credit balance — money the shop is holding for them (see
models.CustomerCredit). Shared by Collect Payment (an overpayment, or using
the balance to pay) and Edit Items (a paid invoice corrected downward)."""
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import accounting, models

# Which way each kind moves the balance.
SIGN = {"overpayment": 1, "invoice_reduced": 1, "applied": -1, "refund": -1}
KIND_LABELS = {
    "overpayment": "Overpayment kept as credit",
    "invoice_reduced": "Invoice corrected below what was paid",
    "applied": "Used to pay an invoice",
    "refund": "Refunded to customer",
}


def balance(db: Session, customer_id: int) -> Decimal:
    if not customer_id:
        return Decimal("0")
    total = (
        db.query(func.coalesce(func.sum(models.CustomerCredit.amount), 0))
        .filter(models.CustomerCredit.customer_id == customer_id)
        .scalar()
    )
    return Decimal(str(total or 0))


def record(db: Session, *, kind: str, amount: Decimal, customer: models.Customer, user=None,
           method: str = None, ref_no: str = None, note: str = None, sale: models.Sale = None,
           settlement: models.ReceivableSettlement = None, when=None) -> models.CustomerCredit:
    """Adds one balance movement and its journal entry. `amount` is positive;
    the sign comes from `kind`. `when` backdates both (a payment collected
    earlier and only encoded now). Doesn't commit."""
    amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    row = models.CustomerCredit(
        customer_id=customer.id, amount=amount * SIGN[kind], kind=kind, method=method, ref_no=ref_no, note=note,
        sale_id=sale.id if sale else None, settlement_id=settlement.id if settlement else None,
        created_by=user.id if user else None,
    )
    if when:
        row.created_at = when
    db.add(row)
    db.flush()
    try:
        entry = accounting.post_customer_credit(
            db, kind=kind, amount=amount, customer=customer, method=method, sale=sale,
            entered_by_id=user.id if user else None, txn_date=when.date() if when else None,
        )
        if entry:
            row.journal_entry_id = entry.id
    except accounting.PostingError:
        pass  # same rule as every other posting: never blocks the transaction itself
    return row


def undo(db: Session, row: models.CustomerCredit, *, user=None, reason: str = None):
    """Reverses the row's journal entry (on its own date) and deletes it."""
    if row.journal_entry_id:
        entry = db.get(models.JournalEntry, row.journal_entry_id)
        if entry and entry.status == "posted":
            accounting.reverse_journal(db, entry, reason=reason, entered_by_id=user.id if user else None,
                                       txn_date=entry.txn_date)
    db.delete(row)
