"""Expense Payment Phase 7: receipt_no (separate from the existing freeform
reference_no), attachment_path for a saved receipt image/PDF, and a
Credit Card Payable Clearing account + EXPENSE_PAY_CREDIT_CARD mapping for
the new "credit_card" payment method (treated as a direct tender — paid,
gone — not a payable with its own settle-later step, per the shop owner's
call).

Revision ID: 0051
Revises: 0050
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("expenses", sa.Column("receipt_no", sa.String(60), nullable=True))
    op.add_column("expenses", sa.Column("attachment_path", sa.String(255), nullable=True))

    accounts_tbl = sa.table(
        "accounts",
        sa.column("code", sa.String), sa.column("name", sa.String),
        sa.column("account_type", sa.String), sa.column("normal_balance", sa.String),
        sa.column("is_system", sa.Boolean), sa.column("system_key", sa.String),
    )
    op.bulk_insert(accounts_tbl, [
        {"code": "1013", "name": "Credit Card Clearing", "account_type": "asset", "normal_balance": "debit",
         "is_system": True, "system_key": "CREDIT_CARD_CLEARING"},
    ])

    conn = op.get_bind()
    account_id = conn.execute(sa.text("SELECT id FROM accounts WHERE system_key = 'CREDIT_CARD_CLEARING'")).scalar()
    mappings_tbl = sa.table(
        "account_mappings",
        sa.column("function_key", sa.String), sa.column("label", sa.String), sa.column("account_id", sa.Integer),
    )
    op.bulk_insert(mappings_tbl, [
        {"function_key": "EXPENSE_PAY_CREDIT_CARD", "label": "Expense paid by Credit Card — credit account", "account_id": account_id},
    ])


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM account_mappings WHERE function_key = 'EXPENSE_PAY_CREDIT_CARD'"))
    conn.execute(sa.text("DELETE FROM accounts WHERE system_key = 'CREDIT_CARD_CLEARING'"))
    op.drop_column("expenses", "attachment_path")
    op.drop_column("expenses", "receipt_no")
