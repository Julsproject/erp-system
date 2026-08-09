"""Accounting Phase 2+ roadmap item 10: Bank Reconciliation. Lets a
BankTransaction be marked reconciled against the actual bank/GCash
statement, by whom and when.

Revision ID: 0046
Revises: 0045
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bank_transactions", sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bank_transactions", sa.Column("reconciled_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True))


def downgrade() -> None:
    op.drop_column("bank_transactions", "reconciled_by_id")
    op.drop_column("bank_transactions", "reconciled_at")
