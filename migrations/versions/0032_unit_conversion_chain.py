"""Let a product's unit ladder chain through another unit instead of only
through base — e.g. Sack relative to Elf Load, Elf Load relative to Forward
Load — resolved into factor_to_base on save so every downstream reader
(POS, purchases, sales, reports) keeps using the same single resolved value.

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-04
"""
from alembic import op
import sqlalchemy as sa

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "product_units",
        sa.Column("relative_to_unit_id", sa.Integer(), sa.ForeignKey("product_units.id"), nullable=True),
    )
    op.add_column(
        "product_units",
        sa.Column("relative_factor", sa.Numeric(14, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("product_units", "relative_factor")
    op.drop_column("product_units", "relative_to_unit_id")
