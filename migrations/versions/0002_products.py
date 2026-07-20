"""inventory: categories, unit_types, products

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-20
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("name", name="uq_categories_name"),
    )
    op.create_table(
        "unit_types",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("name", name="uq_unit_types_name"),
    )
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("category_id", sa.Integer(), sa.ForeignKey("categories.id"), nullable=True),
        sa.Column("unit_type_id", sa.Integer(), sa.ForeignKey("unit_types.id"), nullable=True),
        sa.Column("cost_price", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("selling_price", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("beginning_stock", sa.Numeric(14, 3), server_default="0", nullable=False),
        sa.Column("stock_qty", sa.Numeric(14, 3), server_default="0", nullable=False),
        sa.Column("is_vat", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_products_name", "products", ["name"])


def downgrade() -> None:
    op.drop_index("ix_products_name", table_name="products")
    op.drop_table("products")
    op.drop_table("unit_types")
    op.drop_table("categories")
