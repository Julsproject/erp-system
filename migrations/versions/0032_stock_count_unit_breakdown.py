"""Let a stock count line be entered per selling unit (e.g. how many FORWARD,
Elf, Elf 1/2 physically counted) instead of only one combined base-unit
number — counted_qty stays the single resolved total everything else reads.

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-05
"""
from alembic import op
import sqlalchemy as sa

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stock_count_lines",
        sa.Column("unit_breakdown", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stock_count_lines", "unit_breakdown")
