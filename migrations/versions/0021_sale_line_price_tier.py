"""record which of the three prices a sale line was charged at

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing lines predate the three-price system and were all sold at the
    # single (fixed) price, so "fixed" is the correct backfill.
    op.add_column("sale_lines", sa.Column("price_tier", sa.String(length=10), server_default="fixed"))


def downgrade() -> None:
    op.drop_column("sale_lines", "price_tier")
