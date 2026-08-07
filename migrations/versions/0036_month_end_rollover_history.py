"""Track a per-product line for every month-end rollover, instead of only
one summary row in the Activity Log for the whole batch — lets you answer
"how much got added to THIS item's beginning stock in September" directly.

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-06
"""
from alembic import op
import sqlalchemy as sa

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "month_end_rollover_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("product_name", sa.String(150), nullable=False),
        sa.Column("period", sa.String(7), nullable=False),  # "2026-09"
        sa.Column("qty_moved", sa.Numeric(14, 3), nullable=False),
        sa.Column("old_beginning", sa.Numeric(14, 3), nullable=False),
        sa.Column("new_beginning", sa.Numeric(14, 3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_month_end_rollover_lines_product", "month_end_rollover_lines", ["product_id"])
    op.create_index("ix_month_end_rollover_lines_period", "month_end_rollover_lines", ["period"])


def downgrade() -> None:
    op.drop_index("ix_month_end_rollover_lines_period", table_name="month_end_rollover_lines")
    op.drop_index("ix_month_end_rollover_lines_product", table_name="month_end_rollover_lines")
    op.drop_table("month_end_rollover_lines")
