"""Void a sale: adds the flag/audit columns to sales, plus the setting that
lets an admin/manager allow cashiers to void too.

Revision ID: 0037
Revises: 0036
Create Date: 2026-08-08
"""
from alembic import op
import sqlalchemy as sa

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sales", sa.Column("is_voided", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("sales", sa.Column("void_reason", sa.String(255), nullable=True))
    op.add_column("sales", sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sales", sa.Column("voided_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True))


def downgrade() -> None:
    op.drop_column("sales", "voided_by_id")
    op.drop_column("sales", "voided_at")
    op.drop_column("sales", "void_reason")
    op.drop_column("sales", "is_voided")
