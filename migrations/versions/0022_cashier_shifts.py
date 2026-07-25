"""cashier_shifts — cash drawer opening float + end-of-shift count/variance

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-24
"""
from alembic import op
import sqlalchemy as sa

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cashier_shifts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cashier_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(length=10), server_default="open", nullable=False),
        sa.Column("opening_amount", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expected_cash", sa.Numeric(12, 2), nullable=True),
        sa.Column("counted_cash", sa.Numeric(12, 2), nullable=True),
        sa.Column("variance", sa.Numeric(12, 2), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deposited_amount", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("bank_account_id", sa.Integer(), sa.ForeignKey("bank_accounts.id"), nullable=True),
        sa.Column("bank_transaction_id", sa.Integer(), sa.ForeignKey("bank_transactions.id"), nullable=True),
        sa.Column("notes", sa.String(length=255)),
    )
    op.create_index("ix_cashier_shifts_cashier_id", "cashier_shifts", ["cashier_id"])
    op.create_index("ix_cashier_shifts_status", "cashier_shifts", ["status"])


def downgrade() -> None:
    op.drop_table("cashier_shifts")
