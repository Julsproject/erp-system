"""Add purchase_multiplier to products — scales stock qty received on purchase.

Revision ID: 0031
Revises: 0030
Create Date: 2026-08-03
"""
from alembic import op
import sqlalchemy as sa

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("purchase_multiplier", sa.Numeric(10, 3), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("products", "purchase_multiplier")
