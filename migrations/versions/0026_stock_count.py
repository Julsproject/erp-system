"""stock_counts / stock_count_lines — physical inventory / cycle count

Revision ID: 0026
Revises: 0025
Create Date: 2026-07-26
"""
from alembic import op
import sqlalchemy as sa

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stock_counts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ref_no", sa.String(length=20), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("notes", sa.String(length=255), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_stock_counts_ref_no", "stock_counts", ["ref_no"], unique=True)

    op.create_table(
        "stock_count_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("stock_count_id", sa.Integer(), sa.ForeignKey("stock_counts.id"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("product_name", sa.String(length=150), nullable=False),
        sa.Column("system_qty", sa.Numeric(14, 3), nullable=False),
        sa.Column("counted_qty", sa.Numeric(14, 3), nullable=False, server_default="0"),
        sa.Column("first_scanned_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_stock_count_lines_count", "stock_count_lines", ["stock_count_id"])


def downgrade() -> None:
    op.drop_index("ix_stock_count_lines_count", table_name="stock_count_lines")
    op.drop_table("stock_count_lines")
    op.drop_index("ix_stock_counts_ref_no", table_name="stock_counts")
    op.drop_table("stock_counts")
