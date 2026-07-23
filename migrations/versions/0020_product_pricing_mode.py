"""three selling prices per product: fixed, markup % and margin %

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-23

selling_price stays the fixed price (and the POS default); markup_price and
margin_price are derived from cost using the two percentages.
"""
from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("markup_pct", sa.Numeric(6, 2), server_default="0", nullable=False))
    op.add_column("products", sa.Column("markup_price", sa.Numeric(12, 2), server_default="0", nullable=False))
    op.add_column("products", sa.Column("margin_pct", sa.Numeric(6, 2), server_default="0", nullable=False))
    op.add_column("products", sa.Column("margin_price", sa.Numeric(12, 2), server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("products", "margin_price")
    op.drop_column("products", "margin_pct")
    op.drop_column("products", "markup_price")
    op.drop_column("products", "markup_pct")
