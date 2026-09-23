"""More decimals for base-unit quantities and unit factors, so changing an
item's base unit (Kg -> Box, Meter -> Roll) converts its history exactly:
a meter is 1/150 of a roll = 0.00666667, which Numeric(14, 4) can't hold,
and 0.493 kg is 0.01972 box, which Numeric(14, 3) rounds. Widening never
changes an existing value.

Revision ID: 0075
Revises: 0074
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None

QTY = [  # base-unit quantities -> Numeric(18, 6)
    ("products", "beginning_stock"), ("products", "stock_qty"), ("products", "reorder_level"),
    ("stock_movements", "qty_base"),
    ("inventory_adjustment_lines", "qty_base"), ("inventory_adjustment_lines", "on_hand_before"),
    ("month_end_rollover_lines", "qty_moved"), ("month_end_rollover_lines", "old_beginning"),
    ("month_end_rollover_lines", "new_beginning"),
    ("stock_count_lines", "system_qty"), ("stock_count_lines", "counted_qty"),
]
FACTOR = [  # unit factors -> Numeric(18, 8)
    ("products", "replenish_factor"), ("product_units", "factor_to_base"), ("product_units", "relative_factor"),
    ("sale_lines", "unit_factor"), ("quotation_lines", "unit_factor"), ("purchase_lines", "unit_factor"),
    ("inventory_adjustment_lines", "unit_factor"),
]


def upgrade() -> None:
    for table, col in QTY:
        op.alter_column(table, col, type_=sa.Numeric(18, 6))
    for table, col in FACTOR:
        op.alter_column(table, col, type_=sa.Numeric(18, 8))


def downgrade() -> None:
    for table, col in QTY:
        op.alter_column(table, col, type_=sa.Numeric(14, 3))
    for table, col in FACTOR:
        op.alter_column(table, col, type_=sa.Numeric(14, 4))
