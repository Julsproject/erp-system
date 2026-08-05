"""Drop purchase_multiplier — made redundant now that a purchase unit (e.g.
FORWARD) can just be a proper row in the Units & Conversions ladder, which
already scales stock correctly via its own factor_to_base.

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-05
"""
from alembic import op
import sqlalchemy as sa

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("products", "purchase_multiplier")


def downgrade() -> None:
    op.add_column(
        "products",
        sa.Column("purchase_multiplier", sa.Numeric(10, 3), nullable=False, server_default="1"),
    )
