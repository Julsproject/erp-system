"""products.is_service + products.income_account_id — a service sold at the
POS (renting out the mixer) instead of goods: no stock is taken or counted
and no cost of goods is booked, and its income posts to its own account
(e.g. 4500 Rental Income) rather than Sales. Selling it on an SI still uses
the SI number and records its VAT like any sale.

Revision ID: 0083
Revises: 0082
Create Date: 2026-10-26
"""
from alembic import op
import sqlalchemy as sa

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("is_service", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("products", sa.Column("income_account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=True))


def downgrade() -> None:
    op.drop_column("products", "income_account_id")
    op.drop_column("products", "is_service")
