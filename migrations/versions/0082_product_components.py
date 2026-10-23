"""product_components — a product that is sold as a made-up pair/set, built
from other products the shop actually buys (an expansion bolt "w/ shield" =
1 bolt + 1 shield). Selling one assembles it from its components first, the
same idea as Product.replenish_from_id opening a sealed pack, except the
parts come from several products instead of one.

Revision ID: 0082
Revises: 0081
Create Date: 2026-10-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_components",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False, index=True),
        sa.Column("component_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        # How many of the component's own base units go into one parent.
        sa.Column("qty", sa.Numeric(18, 8), nullable=False, server_default="1"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("product_components")
