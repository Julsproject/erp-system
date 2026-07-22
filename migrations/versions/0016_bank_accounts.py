"""cash & banking: bank accounts and their deposit/withdrawal ledger

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bank_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False, unique=True),
        sa.Column("bank_name", sa.String(length=80)),
        sa.Column("account_no", sa.String(length=60)),
        sa.Column("opening_balance", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "bank_transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("bank_accounts.id"), nullable=False),
        sa.Column("txn_type", sa.String(length=12), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("txn_date", sa.Date(), nullable=False),
        sa.Column("description", sa.String(length=255)),
        sa.Column("reference_no", sa.String(length=60)),
        sa.Column("is_voided", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_bank_transactions_account_id", "bank_transactions", ["account_id"])
    op.create_index("ix_bank_transactions_txn_date", "bank_transactions", ["txn_date"])


def downgrade() -> None:
    op.drop_table("bank_transactions")
    op.drop_table("bank_accounts")
