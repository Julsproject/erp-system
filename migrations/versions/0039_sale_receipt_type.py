"""Sale receipt_type: which physical booklet (DRS/DRB/SI) an invoice # was
written on, so POS can suggest the next number per booklet sequence.

Revision ID: 0039
Revises: 0038
Create Date: 2026-08-08
"""
from alembic import op
import sqlalchemy as sa

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sales", sa.Column("receipt_type", sa.String(10), nullable=True))


def downgrade() -> None:
    op.drop_column("sales", "receipt_type")
