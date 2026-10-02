"""Inventory Adjustment reasons for stock that leaves without a sale:

  Freebie / promo — a give-away with no purchase. Its cost is a selling
    expense (Promotions & Freebies), not shrinkage.
  Owner's use (personal) — the owner takes stock home. Not an expense of
    the business at all: it's a withdrawal, booked to Owner's Drawings
    (equity), so profit isn't understated.

Owner's Drawings is a contra-equity account. It's seeded with a CREDIT
normal balance on purpose: the balance sheet sums equity accounts by their
own normal balance, so a debit balance here reads as a negative and reduces
total equity — which is what drawings do.

Revision ID: 0081
Revises: 0080
Create Date: 2026-10-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None

# (code, name, account_type, normal_balance, system_key)
SEED_ACCOUNTS = [
    ("3300", "Owner's Drawings", "equity", "credit", "OWNERS_DRAWINGS"),
    ("6100", "Promotions & Freebies", "expense", "debit", "PROMOTIONS_EXPENSE"),
]

# (function_key, label, account_system_key)
SEED_MAPPINGS = [
    ("INV_ADJ_FREEBIE", "Inventory adjustment — freebie / promo (no sale)", "PROMOTIONS_EXPENSE"),
    ("INV_ADJ_DRAWINGS", "Inventory adjustment — owner's personal use", "OWNERS_DRAWINGS"),
]


def upgrade() -> None:
    conn = op.get_bind()
    existing = {r[0] for r in conn.execute(sa.text("SELECT system_key FROM accounts WHERE system_key IS NOT NULL"))}
    used_codes = {r[0] for r in conn.execute(sa.text("SELECT code FROM accounts"))}
    accounts_tbl = sa.table(
        "accounts",
        sa.column("code", sa.String), sa.column("name", sa.String), sa.column("account_type", sa.String),
        sa.column("normal_balance", sa.String), sa.column("is_system", sa.Boolean), sa.column("system_key", sa.String),
    )
    rows = []
    for code, name, atype, normal, key in SEED_ACCOUNTS:
        if key in existing:
            continue
        while code in used_codes:  # the owner may already have used the code for their own account
            code = str(int(code) + 1)
        used_codes.add(code)
        rows.append({"code": code, "name": name, "account_type": atype, "normal_balance": normal,
                     "is_system": True, "system_key": key})
    if rows:
        op.bulk_insert(accounts_tbl, rows)

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
    conn.execute(sa.text(
        "DELETE FROM accounts WHERE system_key IN :keys"
        " AND NOT EXISTS (SELECT 1 FROM journal_lines jl WHERE jl.account_id = accounts.id)"
    ).bindparams(sa.bindparam("keys", expanding=True)), {"keys": [a[4] for a in SEED_ACCOUNTS]})
