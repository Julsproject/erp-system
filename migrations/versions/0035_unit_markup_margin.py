"""Give every unit in a product's Units & Conversions ladder the same three
pricing tiers the base unit already has (Fixed/Markup/Margin), not just one
flat price — same math as pricing.markup_price()/margin_price(), applied
against that unit's own cost (base cost x factor_to_base).

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-06
"""
from alembic import op
import sqlalchemy as sa

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("product_units", sa.Column("markup_pct", sa.Numeric(6, 2), nullable=False, server_default="0"))
    op.add_column("product_units", sa.Column("markup_price", sa.Numeric(12, 2), nullable=False, server_default="0"))
    op.add_column("product_units", sa.Column("margin_pct", sa.Numeric(6, 2), nullable=False, server_default="0"))
    op.add_column("product_units", sa.Column("margin_price", sa.Numeric(12, 2), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("product_units", "margin_price")
    op.drop_column("product_units", "margin_pct")
    op.drop_column("product_units", "markup_price")
    op.drop_column("product_units", "markup_pct")
