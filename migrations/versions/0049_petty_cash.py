"""Cash & Banking Phase 3: Petty Cash as a BankAccount.account_kind
("bank" | "ewallet" | "petty_cash") rather than a separate concept —
disbursement/replenishment reuses banking.py's existing deposit/withdrawal
routes and accounting.post_bank_transaction untouched. Expense gets
paid_from_account_id so a petty-cash-paid expense can point at exactly
which box paid it.

Revision ID: 0049
Revises: 0048
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bank_accounts", sa.Column("account_kind", sa.String(20), nullable=False, server_default="bank"))
    op.add_column("expenses", sa.Column("paid_from_account_id", sa.Integer(), sa.ForeignKey("bank_accounts.id"), nullable=True))


def downgrade() -> None:
    op.drop_column("expenses", "paid_from_account_id")
    op.drop_column("bank_accounts", "account_kind")
