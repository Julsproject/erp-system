"""post-dated cheque (PDC) register — received from customers, issued to suppliers

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "post_dated_cheques",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=12), server_default="pending", nullable=False),
        sa.Column("bank", sa.String(length=60)),
        sa.Column("cheque_no", sa.String(length=40)),
        sa.Column("cheque_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("notes", sa.String(length=255)),
        sa.Column("sale_id", sa.Integer(), sa.ForeignKey("sales.id"), nullable=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=True),
        sa.Column("settlement_id", sa.Integer(), sa.ForeignKey("receivable_settlements.id"), nullable=True),
        sa.Column("purchase_id", sa.Integer(), sa.ForeignKey("purchases.id"), nullable=True),
        sa.Column("supplier_id", sa.Integer(), sa.ForeignKey("suppliers.id"), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_pdc_status", "post_dated_cheques", ["status"])
    op.create_index("ix_pdc_direction", "post_dated_cheques", ["direction"])
    op.create_index("ix_pdc_cheque_date", "post_dated_cheques", ["cheque_date"])


def downgrade() -> None:
    op.drop_table("post_dated_cheques")
