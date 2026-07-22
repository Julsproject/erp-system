"""expenses (with categories) and delivery management

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "expense_categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=80), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "expenses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ref_no", sa.String(length=20), unique=True),
        sa.Column("category_id", sa.Integer(), sa.ForeignKey("expense_categories.id"), nullable=True),
        sa.Column("payee", sa.String(length=150)),
        sa.Column("description", sa.String(length=255)),
        sa.Column("amount", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("expense_date", sa.Date(), nullable=False),
        sa.Column("payment_method", sa.String(length=20), server_default="cash", nullable=False),
        sa.Column("reference_no", sa.String(length=60)),
        sa.Column("notes", sa.String(length=255)),
        sa.Column("is_voided", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_expenses_ref_no", "expenses", ["ref_no"])
    op.create_index("ix_expenses_expense_date", "expenses", ["expense_date"])
    op.create_index("ix_expenses_category_id", "expenses", ["category_id"])

    op.create_table(
        "deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("delivery_no", sa.String(length=20), unique=True),
        sa.Column("sale_id", sa.Integer(), sa.ForeignKey("sales.id"), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("recipient_name", sa.String(length=150)),
        sa.Column("address", sa.String(length=255)),
        sa.Column("contact_no", sa.String(length=40)),
        sa.Column("driver_name", sa.String(length=100)),
        sa.Column("vehicle", sa.String(length=60)),
        sa.Column("scheduled_date", sa.Date(), nullable=True),
        sa.Column("notes", sa.String(length=255)),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_deliveries_delivery_no", "deliveries", ["delivery_no"])
    op.create_index("ix_deliveries_status", "deliveries", ["status"])
    op.create_index("ix_deliveries_sale_id", "deliveries", ["sale_id"])


def downgrade() -> None:
    op.drop_table("deliveries")
    op.drop_table("expenses")
    op.drop_table("expense_categories")
