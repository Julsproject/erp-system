"""low-stock threshold, customer credit terms, utang due date, sale cost snapshot

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-21
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Low-stock alerts: per-product threshold in base units (0 = no alert).
    op.add_column("products", sa.Column("reorder_level", sa.Numeric(14, 3), server_default="0", nullable=False))

    # Credit terms per customer, used to compute an utang's due date.
    op.add_column("customers", sa.Column("credit_days", sa.Integer(), server_default="15", nullable=False))

    # When the utang on a sale falls due.
    op.add_column("sales", sa.Column("due_date", sa.Date(), nullable=True))

    # Snapshot of the product's cost (per base unit) at the moment of sale.
    op.add_column("sale_lines", sa.Column("unit_cost", sa.Numeric(12, 2), server_default="0", nullable=False))

    # Backfill: give existing sale lines today's cost so historical profit is at
    # least an estimate rather than 100% margin.
    op.execute(
        """
        UPDATE sale_lines sl
        SET unit_cost = p.cost_price
        FROM products p
        WHERE sl.product_id = p.id AND sl.unit_cost = 0
        """
    )

    # Backfill: existing receivables get a due date of sale date + 15 days.
    op.execute(
        """
        UPDATE sales
        SET due_date = (created_at AT TIME ZONE 'Asia/Manila')::date + INTERVAL '15 days'
        WHERE receivable_amount > 0 AND due_date IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("sale_lines", "unit_cost")
    op.drop_column("sales", "due_date")
    op.drop_column("customers", "credit_days")
    op.drop_column("products", "reorder_level")
