"""customers, split payments, receivable

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("tin", sa.String(length=30)),
        sa.Column("address", sa.String(length=255)),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_customers_name", "customers", ["name"])

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sale_id", sa.Integer(), sa.ForeignKey("sales.id"), nullable=False),
        sa.Column("method", sa.String(length=20), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), server_default="0", nullable=False),
    )
    op.create_index("ix_payments_sale_id", "payments", ["sale_id"])

    op.add_column("sales", sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=True))
    op.add_column("sales", sa.Column("receivable_amount", sa.Numeric(12, 2), server_default="0", nullable=False))
    op.alter_column("sales", "payment_method", type_=sa.String(length=40))


def downgrade() -> None:
    op.alter_column("sales", "payment_method", type_=sa.String(length=20))
    op.drop_column("sales", "receivable_amount")
    op.drop_column("sales", "customer_id")
    op.drop_table("payments")
    op.drop_table("customers")
