"""ReceivableSettlement.ref_no — a generic payment reference (GCash ref,
bank transfer ref, etc.) separate from cheque_no, which already covers
cheque payments specifically.

Revision ID: 0055
Revises: 0054
Create Date: 2026-08-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("receivable_settlements", sa.Column("ref_no", sa.String(60), nullable=True))


def downgrade() -> None:
    op.drop_column("receivable_settlements", "ref_no")
