"""StockCount.count_date — the date the physical count actually happened,
separate from completed_at (when the session got marked done in the
system, which can lag days behind if the shop counts on paper first and
types it in later).

Backfilled from the best available approximation for existing rows
(completed_at's date if the count finished, otherwise created_at's date)
so nothing regresses for past counts until someone corrects it.

Revision ID: 0059
Revises: 0058
Create Date: 2026-09-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("stock_counts", sa.Column("count_date", sa.Date(), nullable=True))
    op.execute(
        "UPDATE stock_counts SET count_date = COALESCE(completed_at::date, created_at::date)"
    )


def downgrade() -> None:
    op.drop_column("stock_counts", "count_date")
