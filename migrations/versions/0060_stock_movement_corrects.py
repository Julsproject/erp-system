"""StockMovement.corrects_movement_id — links a correction movement back to
the original sale-deduction it reverses.

Used by the "void double-deduction" flow (Inventory list -> ⚠ flag on a
product -> pick a date range -> confirm): a past sale's stock effect gets
added back via a new StockMovement, pointed at the original one it corrects.
That link is also how the double-deduction finder knows not to flag a sale
that's already been corrected on a later page load.

Revision ID: 0060
Revises: 0059
Create Date: 2026-09-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stock_movements",
        sa.Column("corrects_movement_id", sa.Integer(), sa.ForeignKey("stock_movements.id"), nullable=True),
    )
    op.create_index(
        "ix_stock_movements_corrects_movement_id", "stock_movements", ["corrects_movement_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_stock_movements_corrects_movement_id", table_name="stock_movements")
    op.drop_column("stock_movements", "corrects_movement_id")
