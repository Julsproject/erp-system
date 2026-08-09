"""Accounting Phase 5: link BankAccount rows to their own ledger account
(gl_account_id), and let a manual BankTransaction record its other side
(contra_account_id) so it can post too. Also the plumbing PDC clearing
needed (added in the same session, no schema change) to actually settle
into the ledger instead of silently posting nothing for a cheque.

Revision ID: 0045
Revises: 0044
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bank_accounts", sa.Column("gl_account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=True, unique=True))
    op.add_column("bank_transactions", sa.Column("contra_account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=True))


def downgrade() -> None:
    op.drop_column("bank_transactions", "contra_account_id")
    op.drop_column("bank_accounts", "gl_account_id")
