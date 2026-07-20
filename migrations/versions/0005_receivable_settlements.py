"""receivable settlements (utang collections)

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "receivable_settlements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sale_id", sa.Integer(), sa.ForeignKey("sales.id"), nullable=False),
        sa.Column("method", sa.String(length=20), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("bank", sa.String(length=60)),
        sa.Column("cheque_no", sa.String(length=40)),
        sa.Column("cheque_date", sa.String(length=20)),
        sa.Column("cashier_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_receivable_settlements_sale_id", "receivable_settlements", ["sale_id"])


def downgrade() -> None:
    op.drop_table("receivable_settlements")
