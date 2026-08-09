"""Cash & Banking Phase 2: Maya + a generic "Other E-Wallet" catch-all as
real payment methods across Sales, Purchases, and Expenses, following the
exact ledger-only pattern GCash already uses (a system_key account, not a
BankAccount row).

Revision ID: 0048
Revises: 0047
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None

# (code, name, account_type, normal_balance, system_key)
SEED_ACCOUNTS = [
    ("1011", "Maya", "asset", "debit", "MAYA"),
    ("1012", "Other E-Wallet", "asset", "debit", "OTHER_EWALLET"),
]

# (function_key, label, account_system_key)
SEED_MAPPINGS = [
    ("SALE_MAYA", "Maya Sale — debit account", "MAYA"),
    ("SALE_OTHER_EWALLET", "Other E-Wallet Sale — debit account", "OTHER_EWALLET"),
    ("PURCHASE_PAY_MAYA", "Supplier paid by Maya — credit account", "MAYA"),
    ("PURCHASE_PAY_OTHER_EWALLET", "Supplier paid by Other E-Wallet — credit account", "OTHER_EWALLET"),
    ("EXPENSE_PAY_MAYA", "Expense paid by Maya — credit account", "MAYA"),
    ("EXPENSE_PAY_OTHER_EWALLET", "Expense paid by Other E-Wallet — credit account", "OTHER_EWALLET"),
]


def upgrade() -> None:
    accounts_tbl = sa.table(
        "accounts",
        sa.column("code", sa.String), sa.column("name", sa.String),
        sa.column("account_type", sa.String), sa.column("normal_balance", sa.String),
        sa.column("is_system", sa.Boolean), sa.column("system_key", sa.String),
    )
    op.bulk_insert(accounts_tbl, [
        {"code": code, "name": name, "account_type": atype, "normal_balance": bal,
         "is_system": True, "system_key": key}
        for code, name, atype, bal, key in SEED_ACCOUNTS
    ])

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
    sys_keys = tuple(a[4] for a in SEED_ACCOUNTS)
    conn.execute(sa.text("DELETE FROM accounts WHERE system_key IN :keys").bindparams(
        sa.bindparam("keys", expanding=True)
    ), {"keys": list(sys_keys)})
