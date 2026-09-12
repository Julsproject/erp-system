"""expenses.tin — the payee's TIN, for the Input VAT report's TIN # column.

An Expense has no linked Supplier record (payee is free text), so unlike a
Purchase's Supplier.tin, this has to be captured per-expense. Before this,
_vat_input_detail always left an expense row's TIN blank.

Revision ID: 0067
Revises: 0066
Create Date: 2026-09-12
"""
from alembic import op
import sqlalchemy as sa

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("expenses", sa.Column("tin", sa.String(30), nullable=True))


def downgrade() -> None:
    op.drop_column("expenses", "tin")
