"""link a purchase return back to the delivery it came from

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("purchases", sa.Column("original_purchase_id", sa.Integer(), sa.ForeignKey("purchases.id"), nullable=True))
    op.create_index("ix_purchases_original_purchase_id", "purchases", ["original_purchase_id"])


def downgrade() -> None:
    op.drop_index("ix_purchases_original_purchase_id", table_name="purchases")
    op.drop_column("purchases", "original_purchase_id")
