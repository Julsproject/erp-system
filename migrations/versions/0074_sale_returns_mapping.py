"""Refunds and exchanges post to the books: the account a returned sale is
debited to. Sales Returns & Allowances was seeded in 0042 and never mapped.

Revision ID: 0074
Revises: 0073
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None

SEED_MAPPINGS = [
    ("SALE_RETURNS", "Refund / exchange — returned goods (debit)", "SALES_RETURNS"),
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
