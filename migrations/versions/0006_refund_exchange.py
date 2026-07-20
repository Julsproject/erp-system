"""refund/exchange transaction type

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sales", sa.Column("txn_type", sa.String(length=12), server_default="sale", nullable=False))
    op.add_column("sales", sa.Column("original_sale_id", sa.Integer(), sa.ForeignKey("sales.id"), nullable=True))


def downgrade() -> None:
    op.drop_column("sales", "original_sale_id")
    op.drop_column("sales", "txn_type")
