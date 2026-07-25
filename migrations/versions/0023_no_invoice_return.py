"""sales.no_invoice_return — refund/exchange sourced from inventory search
instead of a matched invoice

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-24
"""
from alembic import op
import sqlalchemy as sa

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sales", sa.Column("no_invoice_return", sa.Boolean(), server_default="false", nullable=False))


def downgrade() -> None:
    op.drop_column("sales", "no_invoice_return")
