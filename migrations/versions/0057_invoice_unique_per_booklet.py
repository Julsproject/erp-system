"""Invoice numbers are unique PER BOOKLET, not globally.

Each receipt booklet (DRS/DRB/SI/...) is its own physical pad with its own
numbering, so DRS 255 and DRB 255 are two genuinely different receipts and
both have to be enterable. The old global unique index on invoice_no alone
made the second one impossible to save.

COALESCE(receipt_type, '') rather than a plain two-column index because
Postgres treats NULLs as distinct in a UNIQUE index — without it, sales
with no booklet set could repeat the same number freely.

Going from a stricter rule to a looser one, so no existing row can conflict.

Revision ID: 0057
Revises: 0056
Create Date: 2026-08-21
"""
from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_sales_invoice_no", table_name="sales")
    op.create_index("ix_sales_invoice_no", "sales", ["invoice_no"], unique=False)
    op.execute(
        "CREATE UNIQUE INDEX uq_sales_receipt_type_invoice_no "
        "ON sales (COALESCE(receipt_type, ''), invoice_no)"
    )


def downgrade() -> None:
    # Fails if any per-booklet duplicates were created while this was live —
    # that's correct, they'd have to be resolved by hand before going back to
    # a globally unique rule.
    op.execute("DROP INDEX IF EXISTS uq_sales_receipt_type_invoice_no")
    op.drop_index("ix_sales_invoice_no", table_name="sales")
    op.create_index("ix_sales_invoice_no", "sales", ["invoice_no"], unique=True)
