"""products.no_restock — "Don't restock / order on request". Such an item is
left off Low Stock and No Stock (and the dashboard counts) because running
out of it is expected, not a reorder signal.

Revision ID: 0080
Revises: 0079
Create Date: 2026-10-01
"""
from alembic import op
import sqlalchemy as sa

revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("no_restock", sa.Boolean(), nullable=False, server_default="false"))


def downgrade() -> None:
    op.drop_column("products", "no_restock")
