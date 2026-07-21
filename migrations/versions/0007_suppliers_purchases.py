"""suppliers, purchases (receiving / returns), purchase lines

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-21
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "suppliers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=30)),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("contact_person", sa.String(length=120)),
        sa.Column("mobile", sa.String(length=40)),
        sa.Column("telephone", sa.String(length=40)),
        sa.Column("email", sa.String(length=120)),
        sa.Column("address", sa.String(length=255)),
        sa.Column("tin", sa.String(length=30)),
        sa.Column("payment_terms", sa.String(length=60)),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_suppliers_name", "suppliers", ["name"])
    op.create_index("ix_suppliers_code", "suppliers", ["code"], unique=True)

    op.create_table(
        "purchases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ref_no", sa.String(length=30)),
        sa.Column("txn_type", sa.String(length=12), server_default="receive", nullable=False),
        sa.Column("supplier_id", sa.Integer(), sa.ForeignKey("suppliers.id"), nullable=True),
        sa.Column("invoice_no", sa.String(length=40)),
        sa.Column("delivery_date", sa.String(length=20)),
        sa.Column("notes", sa.String(length=255)),
        sa.Column("total", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_purchases_ref_no", "purchases", ["ref_no"], unique=True)
    op.create_index("ix_purchases_supplier_id", "purchases", ["supplier_id"])

    op.create_table(
        "purchase_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("purchase_id", sa.Integer(), sa.ForeignKey("purchases.id"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=True),
        sa.Column("product_name", sa.String(length=150), nullable=False),
        sa.Column("unit_name", sa.String(length=40)),
        sa.Column("unit_factor", sa.Numeric(14, 4), server_default="1", nullable=False),
        sa.Column("qty", sa.Numeric(14, 3), server_default="0", nullable=False),
        sa.Column("unit_cost", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("line_total", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("old_cost", sa.Numeric(12, 4), server_default="0"),
        sa.Column("new_cost", sa.Numeric(12, 4), server_default="0"),
    )
    op.create_index("ix_purchase_lines_purchase_id", "purchase_lines", ["purchase_id"])
    op.create_index("ix_purchase_lines_product_id", "purchase_lines", ["product_id"])


def downgrade() -> None:
    op.drop_table("purchase_lines")
    op.drop_table("purchases")
    op.drop_table("suppliers")
