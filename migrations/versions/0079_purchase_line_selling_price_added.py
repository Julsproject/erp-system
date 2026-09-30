"""purchase_lines.selling_price_added — the per-base amount a delivery's
"Add to selling price" checkbox raised the item's selling price by (its cost
increase). NULL = not applied. Kept so unticking takes off exactly what was
added, and so the same increase can't be added twice.

Revision ID: 0079
Revises: 0078
Create Date: 2026-09-30
"""
from alembic import op
import sqlalchemy as sa

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("purchase_lines", sa.Column("selling_price_added", sa.Numeric(12, 4), nullable=True))


def downgrade() -> None:
    op.drop_column("purchase_lines", "selling_price_added")
