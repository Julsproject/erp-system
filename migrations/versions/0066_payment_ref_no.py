"""payments.ref_no — GCash/bank-transfer/other reference for a sale's own
payment, captured at checkout time (not a credit collection, which already
had this on receivable_settlements) so it can be traced back the same way
an invoice # can.

Revision ID: 0066
Revises: 0065
Create Date: 2026-09-11
"""
from alembic import op
import sqlalchemy as sa

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payments", sa.Column("ref_no", sa.String(60), nullable=True))


def downgrade() -> None:
    op.drop_column("payments", "ref_no")
