"""Sub Categories — a finer grouping under Category, for Inventory filtering
and Excel import/export.

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-01
"""
from alembic import op
import sqlalchemy as sa

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sub_categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=80), nullable=False, unique=True),
        sa.Column("category_id", sa.Integer(), sa.ForeignKey("categories.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.add_column("products", sa.Column("subcategory_id", sa.Integer(), sa.ForeignKey("sub_categories.id"), nullable=True))


def downgrade() -> None:
    op.drop_column("products", "subcategory_id")
    op.drop_table("sub_categories")
