"""quotations (estimates) with a pending/confirmed/paid/cancelled lifecycle

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quotations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("quote_no", sa.String(length=20)),
        sa.Column("status", sa.String(length=12), server_default="pending", nullable=False),
        sa.Column("customer_name", sa.String(length=150)),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=True),
        sa.Column("vat_applied", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("subtotal", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("discount_total", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("vat_amount", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("total", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("converted_sale_id", sa.Integer(), sa.ForeignKey("sales.id"), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_quotations_quote_no", "quotations", ["quote_no"], unique=True)
    op.create_index("ix_quotations_status", "quotations", ["status"])

    op.create_table(
        "quotation_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("quotation_id", sa.Integer(), sa.ForeignKey("quotations.id"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=True),
        sa.Column("product_name", sa.String(length=150), nullable=False),
        sa.Column("unit_name", sa.String(length=40)),
        sa.Column("unit_factor", sa.Numeric(14, 4), server_default="1", nullable=False),
        sa.Column("qty", sa.Numeric(14, 3), server_default="0", nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("discount", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("line_total", sa.Numeric(12, 2), server_default="0", nullable=False),
    )
    op.create_index("ix_quotation_lines_quotation_id", "quotation_lines", ["quotation_id"])


def downgrade() -> None:
    op.drop_table("quotation_lines")
    op.drop_table("quotations")
