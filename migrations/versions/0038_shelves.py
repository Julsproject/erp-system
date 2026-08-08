"""Shelves: where a product physically sits in the store, so staff can
browse/count by physical location instead of hunting an alphabetical list.

Revision ID: 0038
Revises: 0037
Create Date: 2026-08-08
"""
from alembic import op
import sqlalchemy as sa

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shelves",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.add_column("products", sa.Column("shelf_id", sa.Integer(), sa.ForeignKey("shelves.id"), nullable=True))
    op.create_index("ix_products_shelf_id", "products", ["shelf_id"])


def downgrade() -> None:
    op.drop_index("ix_products_shelf_id", table_name="products")
    op.drop_column("products", "shelf_id")
    op.drop_table("shelves")
