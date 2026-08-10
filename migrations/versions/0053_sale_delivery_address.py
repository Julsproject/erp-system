"""Sale.delivery_address (auto-filled from the customer's own address when
on file, but editable per sale without changing their record) and
Sale.notes (freeform additional instructions), both shown on the receipt.

Revision ID: 0053
Revises: 0052
Create Date: 2026-08-10
"""
from alembic import op
import sqlalchemy as sa

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sales", sa.Column("delivery_address", sa.String(255), nullable=True))
    op.add_column("sales", sa.Column("notes", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("sales", "notes")
    op.drop_column("sales", "delivery_address")
