"""Cancelled/spoiled receipt tracking (DRS/DRB/SI) — so a booklet's number
sequence stays fully accounted for even when a number was never used for
a real sale, which is what matters for BIR compliance.

Revision ID: 0052
Revises: 0051
Create Date: 2026-08-10
"""
from alembic import op
import sqlalchemy as sa

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cancelled_receipts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("receipt_type", sa.String(10), nullable=False),
        sa.Column("invoice_no", sa.String(20), nullable=False),
        sa.Column("cancelled_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column("recorded_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_cancelled_receipts_type_invoice", "cancelled_receipts", ["receipt_type", "invoice_no"])


def downgrade() -> None:
    op.drop_index("ix_cancelled_receipts_type_invoice", table_name="cancelled_receipts")
    op.drop_table("cancelled_receipts")
