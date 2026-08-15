"""Purchase.vat_amount and Purchase.net_amount — VAT-inclusive split on a
receive, same convention as Sale.vat_amount/net_amount, so a VAT-registered
supplier delivery can post its Input VAT split instead of the flat total.

Revision ID: 0054
Revises: 0053
Create Date: 2026-08-14
"""
from alembic import op
import sqlalchemy as sa

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("purchases", sa.Column("vat_amount", sa.Numeric(12, 2), nullable=False, server_default="0"))
    op.add_column("purchases", sa.Column("net_amount", sa.Numeric(12, 2), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("purchases", "net_amount")
    op.drop_column("purchases", "vat_amount")
