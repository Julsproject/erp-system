"""purchase_settlements — the Accounts Payable mirror of
receivable_settlements, so a payable can be paid off in partial
installments instead of all-or-nothing.

Revision ID: 0028
Revises: 0027
Create Date: 2026-07-29
"""
from alembic import op
import sqlalchemy as sa

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "purchase_settlements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("purchase_id", sa.Integer(), sa.ForeignKey("purchases.id"), nullable=False),
        sa.Column("method", sa.String(length=20), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("bank", sa.String(length=60)),
        sa.Column("cheque_no", sa.String(length=40)),
        sa.Column("cheque_date", sa.String(length=20)),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_purchase_settlements_purchase_id", "purchase_settlements", ["purchase_id"])


def downgrade() -> None:
    op.drop_index("ix_purchase_settlements_purchase_id", table_name="purchase_settlements")
    op.drop_table("purchase_settlements")
