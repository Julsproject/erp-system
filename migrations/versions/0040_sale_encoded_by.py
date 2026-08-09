"""Sale encoded_by: optional free-text name of whoever actually wrote up
the sale, separate from the logged-in cashier account.

Revision ID: 0040
Revises: 0039
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sales", sa.Column("encoded_by", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("sales", "encoded_by")
