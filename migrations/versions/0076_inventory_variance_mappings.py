"""Mappings for keeping the ledger's Inventory in step with the shelf:

  INV_COST_VARIANCE — a delivery re-averaging an item's cost while its stock
    is negative (or a pack opened into an Open/Retail item resetting its
    cost) changes what the goods already sold really cost; that difference
    is cost of sales.
  INV_TRUE_UP — Reconcile Sales' "true up" of whatever difference is left
    between the ledger and stock on hand x cost.

Revision ID: 0076
Revises: 0075
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None

SEED_MAPPINGS = [
    ("INV_COST_VARIANCE", "Inventory cost variance (re-averaged cost on stock already sold)", "COST_OF_SALES"),
    ("INV_TRUE_UP", "Inventory true-up to stock on hand (Reconcile Sales)", "INVENTORY_CORRECTION"),
]


def upgrade() -> None:
    conn = op.get_bind()
    key_to_id = dict(conn.execute(sa.text("SELECT system_key, id FROM accounts WHERE system_key IS NOT NULL")).fetchall())
    mapped = {r[0] for r in conn.execute(sa.text("SELECT function_key FROM account_mappings"))}
    mappings_tbl = sa.table(
        "account_mappings",
        sa.column("function_key", sa.String), sa.column("label", sa.String), sa.column("account_id", sa.Integer),
    )
    op.bulk_insert(mappings_tbl, [
        {"function_key": fkey, "label": label, "account_id": key_to_id[akey]}
        for fkey, label, akey in SEED_MAPPINGS if fkey not in mapped
    ])


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM account_mappings WHERE function_key IN :keys").bindparams(
        sa.bindparam("keys", expanding=True)
    ), {"keys": [m[0] for m in SEED_MAPPINGS]})
