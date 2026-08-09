"""Encoders: a managed name list (separate from Users/logins) for who
actually wrote up a sale — replaces the free-text sales.encoded_by column
added in 0040 (never used in production, safe to drop) with a proper FK to
a dropdown-picked list, so entries stay consistent instead of typed freehand.

Revision ID: 0041
Revises: 0040
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "encoders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False, unique=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.drop_column("sales", "encoded_by")
    op.add_column("sales", sa.Column("encoded_by_id", sa.Integer(), sa.ForeignKey("encoders.id"), nullable=True))
    op.create_index("ix_sales_encoded_by_id", "sales", ["encoded_by_id"])


def downgrade() -> None:
    op.drop_index("ix_sales_encoded_by_id", table_name="sales")
    op.drop_column("sales", "encoded_by_id")
    op.add_column("sales", sa.Column("encoded_by", sa.String(100), nullable=True))
    op.drop_table("encoders")
