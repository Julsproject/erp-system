"""Sale.entered_at — the real clock moment this row was written to the
database, never touched by backdating (unlike created_at, which a backdated
sale overwrites with the chosen past transaction date — see
pos._finalize_sale). Lets "how many sales did this cashier actually type in
today" be answered even when most of what they're encoding is backlog.

Backfilled from created_at for existing rows — the best available
approximation, since a past backdated sale's true original entry moment
isn't recoverable after the fact (same reasoning as 0059's count_date
backfill).

Revision ID: 0062
Revises: 0061
Create Date: 2026-09-04
"""
from alembic import op
import sqlalchemy as sa

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sales",
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()),
    )
    op.execute("UPDATE sales SET entered_at = created_at WHERE entered_at IS NULL")


def downgrade() -> None:
    op.drop_column("sales", "entered_at")
