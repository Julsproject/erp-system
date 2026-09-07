"""Product.replenish_from_id — links an "open/retail" counterpart product
(e.g. loose Kg sold off a sealed Bag product) back to its sealed source.
A sale against the open product that would exhaust its own stock instead
pulls a whole pack over from the source first (see pos._replenish_from_
source), so the two products' Stock Cards each stay clean instead of one
product's history mixing whole-pack and loose-retail movements together.

Revision ID: 0063
Revises: 0062
Create Date: 2026-09-08
"""
from alembic import op
import sqlalchemy as sa

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("replenish_from_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=True))


def downgrade() -> None:
    op.drop_column("products", "replenish_from_id")
