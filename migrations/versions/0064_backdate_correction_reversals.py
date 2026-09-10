"""Backdate stranded correction reversals to their original entry's date.

A payment-method or VAT correction (pos.change_payment_method,
purchases.change_payment_method, purchases.edit_purchase_details) reverses
the original journal entry and immediately re-posts a corrected one. Before
this fix, accounting.reverse_journal always dated the reversal "today"
while the re-post kept the entry's original transaction date — so the
mistake and its fix landed in different reporting periods instead of
netting to zero together. Any VAT report (or other period report) run for
a window that includes the original date but ends before the correction
date shows the same invoice's Output/Input VAT twice, and one that ends
between the two genuinely overstates the period's VAT.

This backfills the already-affected reversals: any reversal of a
'reversed' sale/purchase entry, dated differently from the entry it
reverses, where a live replacement entry exists for the same source — the
signature of a correction pair rather than a standalone void (a void has
no replacement, and its later reversal date is the real cancellation date,
not a mistake). Re-dating it to match the entry it reverses collapses the
pair back to net zero within the original period, leaving only the single
corrected entry visible everywhere.

Not reversible — the original (wrong) reversal date isn't recoverable
after the fact, same as 0059/0062's backfills.

Revision ID: 0064
Revises: 0063
Create Date: 2026-09-10
"""
from alembic import op

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE journal_entries AS rev
        SET txn_date = orig.txn_date
        FROM journal_entries AS orig
        WHERE rev.is_reversal_of_id = orig.id
          AND orig.status = 'reversed'
          AND orig.source_type IN ('sale', 'purchase')
          AND rev.txn_date <> orig.txn_date
          AND EXISTS (
            SELECT 1 FROM journal_entries AS repl
            WHERE repl.source_type = orig.source_type
              AND repl.source_id = orig.source_id
              AND repl.status = 'posted'
              AND repl.id <> orig.id
          )
    """)


def downgrade() -> None:
    pass  # the pre-fix reversal dates aren't recoverable
