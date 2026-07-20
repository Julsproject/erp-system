"""units ladder + sales (POS)

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_units",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("factor_to_base", sa.Numeric(14, 4), server_default="1", nullable=False),
        sa.Column("price", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_index("ix_product_units_product_id", "product_units", ["product_id"])

    op.create_table(
        "sales",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("invoice_no", sa.String(length=20)),
        sa.Column("customer_name", sa.String(length=150)),
        sa.Column("cashier_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("subtotal", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("discount_total", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("vat_amount", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("net_amount", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("total", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("payment_method", sa.String(length=20)),
        sa.Column("amount_tendered", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("change_amount", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_sales_invoice_no", "sales", ["invoice_no"], unique=True)

    op.create_table(
        "sale_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sale_id", sa.Integer(), sa.ForeignKey("sales.id"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=True),
        sa.Column("product_name", sa.String(length=150), nullable=False),
        sa.Column("unit_name", sa.String(length=40)),
        sa.Column("unit_factor", sa.Numeric(14, 4), server_default="1", nullable=False),
        sa.Column("qty", sa.Numeric(14, 3), server_default="0", nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("discount", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("line_total", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("is_vat", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_index("ix_sale_lines_sale_id", "sale_lines", ["sale_id"])

    op.create_table(
        "stock_movements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("qty_base", sa.Numeric(14, 3), nullable=False),
        sa.Column("reason", sa.String(length=30), nullable=False),
        sa.Column("ref", sa.String(length=30)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_stock_movements_product_id", "stock_movements", ["product_id"])


def downgrade() -> None:
    op.drop_table("stock_movements")
    op.drop_table("sale_lines")
    op.drop_table("sales")
    op.drop_table("product_units")
