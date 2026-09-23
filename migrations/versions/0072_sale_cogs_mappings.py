"""Sales post their cost: account mappings for the Cost of Sales leg
(Dr Cost of Sales / Cr Inventory) that post_sale now adds to every sale's
journal entry. Both accounts were seeded in 0042 and just never had a
function_key pointed at them.

Revision ID: 0072
Revises: 0071
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None

# (function_key, label, account_system_key)
SEED_MAPPINGS = [
    ("SALE_COGS", "Sale — cost of goods sold (debit)", "COST_OF_SALES"),
    ("SALE_INVENTORY", "Sale — inventory taken off the shelf (credit)", "INVENTORY_MERCHANDISE"),
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
