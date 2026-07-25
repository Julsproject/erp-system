"""products.barcode — scan a manufacturer barcode to find a product at POS,
purchases, quotations, etc.

Revision ID: 0025
Revises: 0024
Create Date: 2026-07-26
"""
from alembic import op
import sqlalchemy as sa

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("barcode", sa.String(length=64), nullable=True))
    op.create_index("ix_products_barcode", "products", ["barcode"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_products_barcode", table_name="products")
    op.drop_column("products", "barcode")
