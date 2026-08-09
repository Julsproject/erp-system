"""Accounting Phase 3: Expense gets a vat_amount field and ExpenseCategory
gets an optional account_id, plus the account mappings Expenses posting
needs (a default Expense account, per-method payment accounts, Input VAT).

Revision ID: 0044
Revises: 0043
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

SEED_MAPPINGS = [
    ("EXPENSE_DEFAULT", "Expense — fallback debit account (no category mapping set)", "EXPENSES"),
    ("EXPENSE_PAY_CASH", "Expense paid by Cash — credit account", "CASH_ON_HAND"),
    ("EXPENSE_PAY_GCASH", "Expense paid by GCash — credit account", "GCASH"),
    ("EXPENSE_PAY_BANK_TRANSFER", "Expense paid by Bank Transfer — credit account", "BANK"),
    ("EXPENSE_PAY_CHEQUE", "Expense paid by Cheque — credit account", "BANK"),
    ("INPUT_VAT", "VAT paid on a Purchase/Expense — debit account", "INPUT_VAT"),
]


def upgrade() -> None:
    op.add_column("expense_categories", sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=True))
    op.add_column("expenses", sa.Column("vat_amount", sa.Numeric(12, 2), nullable=False, server_default="0"))

    conn = op.get_bind()
    key_to_id = dict(conn.execute(sa.text("SELECT system_key, id FROM accounts")).fetchall())
    mappings_tbl = sa.table(
        "account_mappings",
        sa.column("function_key", sa.String), sa.column("label", sa.String), sa.column("account_id", sa.Integer),
    )
    op.bulk_insert(mappings_tbl, [
        {"function_key": fkey, "label": label, "account_id": key_to_id[akey]}
        for fkey, label, akey in SEED_MAPPINGS
    ])


def downgrade() -> None:
    conn = op.get_bind()
    keys = tuple(m[0] for m in SEED_MAPPINGS)
    conn.execute(sa.text("DELETE FROM account_mappings WHERE function_key IN :keys").bindparams(
        sa.bindparam("keys", expanding=True)
    ), {"keys": list(keys)})
    op.drop_column("expenses", "vat_amount")
    op.drop_column("expense_categories", "account_id")
