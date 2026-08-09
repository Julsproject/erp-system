"""Accounting Phase 2: account mappings for Purchases posting (receive,
return, and settling a payable). No new accounts needed — AP and
Inventory-Merchandise were already seeded in 0042, just never had a
function_key mapping pointed at them yet.

Revision ID: 0043
Revises: 0042
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

# (function_key, label, account_system_key)
SEED_MAPPINGS = [
    ("INVENTORY_MERCHANDISE", "Purchase receive — debit account", "INVENTORY_MERCHANDISE"),
    ("AP", "Purchase on credit / supplier payment — Accounts Payable", "AP"),
    ("PURCHASE_PAY_CASH", "Supplier paid by Cash — credit account", "CASH_ON_HAND"),
    ("PURCHASE_PAY_GCASH", "Supplier paid by GCash — credit account", "GCASH"),
    ("PURCHASE_PAY_BANK_TRANSFER", "Supplier paid by Bank Transfer — credit account", "BANK"),
    ("PURCHASE_PAY_CHEQUE", "Supplier paid by Cheque — credit account", "BANK"),
    ("PURCHASE_PAY_OTHER", "Supplier paid by Other method — credit account", "CASH_ON_HAND"),
]


def upgrade() -> None:
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
