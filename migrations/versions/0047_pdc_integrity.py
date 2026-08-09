"""PDC integrity: a "deposited" status (physical custody marker between
pending and cleared/bounced) needs a deposit_date to go with it. The
"deposited" value itself needs no schema change — status stays String(12).

Revision ID: 0047
Revises: 0046
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("post_dated_cheques", sa.Column("deposit_date", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("post_dated_cheques", "deposit_date")
