"""Product.replenish_factor — on an open/retail counterpart whose sealed
source is counted in a different base unit (e.g. SKIMCOAT ABC sealed by the
bag, its Open/Retail counterpart by the Kg): how many of the counterpart's
own base units one of the source's base units opens into ("1 bag opens into
20 Kg"). pos._replenish_from_source uses it to auto-open a whole bag when
the source has no bigger ladder unit to take the pack size from.

Revision ID: 0068
Revises: 0067
Create Date: 2026-09-15
"""
from alembic import op
import sqlalchemy as sa

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("replenish_factor", sa.Numeric(14, 4), nullable=True))


def downgrade() -> None:
    op.drop_column("products", "replenish_factor")
