"""drop cash_shifts (replaced by automatic cashier activity history)

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("cash_shifts")


def downgrade() -> None:
    op.create_table(
        "cash_shifts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("opening_amount", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("closing_amount", sa.Numeric(12, 2)),
        sa.Column("expected_amount", sa.Numeric(12, 2)),
        sa.Column("difference", sa.Numeric(12, 2)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("notes", sa.String(length=255)),
    )
    op.create_index("ix_cash_shifts_user_id", "cash_shifts", ["user_id"])
    op.create_index("ix_cash_shifts_closed_at", "cash_shifts", ["closed_at"])
