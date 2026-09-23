"""Inventory Adjustments: one document that corrects an item's stock and/or
its cost and posts the peso effect to the books in the same step.

Adds the adjustment header/line tables, links each stock movement back to
the adjustment that wrote it (so a cancel reverses exactly those rows), and
seeds the accounts the posting engine needs:

  3200 Inventory Corrections  (equity) — encoding / opening-balance and cost
       corrections: fixing a record, not a real loss, so it stays off P&L.
  5100 Inventory Shrinkage & Losses (cost of sales) — damage, theft, expiry,
       count shortages.
  4900 Inventory Gain (revenue) — stock found over what the system said.

Revision ID: 0071
Revises: 0070
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None

# (code, name, account_type, normal_balance, system_key)
SEED_ACCOUNTS = [
    ("3200", "Inventory Corrections", "equity", "credit", "INVENTORY_CORRECTION"),
    ("4900", "Inventory Gain", "revenue", "credit", "INVENTORY_GAIN"),
    ("5100", "Inventory Shrinkage & Losses", "cost_of_sales", "debit", "INVENTORY_SHRINKAGE"),
]

# (function_key, label, account_system_key)
SEED_MAPPINGS = [
    ("INV_ADJ_INVENTORY", "Inventory adjustment — inventory account", "INVENTORY_MERCHANDISE"),
    ("INV_ADJ_LOSS", "Inventory adjustment — loss (damage, theft, expired, shortage)", "INVENTORY_SHRINKAGE"),
    ("INV_ADJ_GAIN", "Inventory adjustment — gain (found / extra stock)", "INVENTORY_GAIN"),
    ("INV_ADJ_CORRECTION", "Inventory adjustment — encoding / opening-balance correction", "INVENTORY_CORRECTION"),
    ("INV_REVALUATION", "Inventory cost correction (revaluation)", "INVENTORY_CORRECTION"),
]


def upgrade() -> None:
    op.create_table(
        "inventory_adjustments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ref_no", sa.String(20), unique=True),
        sa.Column("adj_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(30), nullable=False),
        sa.Column("notes", sa.String(255)),
        sa.Column("status", sa.String(12), nullable=False, server_default="draft"),
        sa.Column("source", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("posted_by", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_by", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_reason", sa.String(255)),
        sa.Column("journal_entry_id", sa.Integer(), sa.ForeignKey("journal_entries.id")),
        sa.Column("cancel_journal_entry_id", sa.Integer(), sa.ForeignKey("journal_entries.id")),
    )
    op.create_table(
        "inventory_adjustment_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("adjustment_id", sa.Integer(), sa.ForeignKey("inventory_adjustments.id"), nullable=False, index=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("product_name", sa.String(150), nullable=False),
        sa.Column("unit_name", sa.String(40)),
        sa.Column("unit_factor", sa.Numeric(14, 4), nullable=False, server_default="1"),
        sa.Column("mode", sa.String(8), nullable=False, server_default="delta"),
        sa.Column("qty_input", sa.Numeric(14, 3)),
        sa.Column("new_cost", sa.Numeric(12, 2)),
        sa.Column("note", sa.String(255)),
        sa.Column("qty_base", sa.Numeric(14, 3)),
        sa.Column("on_hand_before", sa.Numeric(14, 3)),
        sa.Column("old_cost", sa.Numeric(12, 2)),
        sa.Column("value_qty", sa.Numeric(14, 2)),
        sa.Column("value_reval", sa.Numeric(14, 2)),
    )
    with op.batch_alter_table("stock_movements") as batch:
        batch.add_column(sa.Column("inventory_adjustment_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_stock_movements_inventory_adjustment_id", "inventory_adjustments",
            ["inventory_adjustment_id"], ["id"])
    op.create_index("ix_stock_movements_inventory_adjustment_id", "stock_movements", ["inventory_adjustment_id"])

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
        "DELETE FROM accounts WHERE system_key IN :keys AND id NOT IN (SELECT account_id FROM journal_lines)"
    ).bindparams(sa.bindparam("keys", expanding=True)), {"keys": [a[4] for a in SEED_ACCOUNTS]})
    op.drop_index("ix_stock_movements_inventory_adjustment_id", table_name="stock_movements")
    with op.batch_alter_table("stock_movements") as batch:
        batch.drop_constraint("fk_stock_movements_inventory_adjustment_id", type_="foreignkey")
        batch.drop_column("inventory_adjustment_id")
    op.drop_table("inventory_adjustment_lines")
    op.drop_table("inventory_adjustments")
