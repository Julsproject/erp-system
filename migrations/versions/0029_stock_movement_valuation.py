"""stock_movements valuation — put a peso value on inventory adjustments
(manual edits + stock counts) so shrinkage/gains show up in P&L instead of
silently correcting quantity with no financial trace.

Revision ID: 0029
Revises: 0028
Create Date: 2026-07-29
"""
from alembic import op
import sqlalchemy as sa

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("stock_movements", sa.Column("unit_cost", sa.Numeric(12, 2), nullable=True))
    op.add_column("stock_movements", sa.Column("value", sa.Numeric(14, 2), nullable=True))
    op.add_column("stock_movements", sa.Column("note", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("stock_movements", "note")
    op.drop_column("stock_movements", "value")
    op.drop_column("stock_movements", "unit_cost")
