"""purchase status lifecycle: pending / confirmed / paid / cancelled

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("purchases", sa.Column("status", sa.String(length=12), server_default="pending", nullable=False))
    op.add_column("purchases", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("purchases", sa.Column("payment_method", sa.String(length=20), nullable=True))
    op.add_column("purchases", sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("purchases", sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))

    # Every purchase created before this migration already had its stock/cost
    # effect applied immediately at save time — so under the new lifecycle
    # they are all already "confirmed" as of when they were created.
    op.execute("UPDATE purchases SET status = 'confirmed', confirmed_at = created_at")


def downgrade() -> None:
    op.drop_column("purchases", "cancelled_at")
    op.drop_column("purchases", "paid_at")
    op.drop_column("purchases", "payment_method")
    op.drop_column("purchases", "confirmed_at")
    op.drop_column("purchases", "status")
