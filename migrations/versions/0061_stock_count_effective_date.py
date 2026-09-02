"""StockCount.effective_date / effective_applied_at — the date Accounting
chooses for when a completed count's result becomes the new Actual Beginning.

Sales and the count's own variance correction never touch Actual Beginning
directly (see _apply_stock_count_correction / _deduct_stock — unchanged);
they only ever move Stocks Qty. Actual Beginning only changes at a deliberate
"close the period" moment — either the existing calendar Month-End Rollover,
or, now, this per-count Effective Date: on/after that date, the count's
covered products get their current Stocks Qty folded into Actual Beginning
and Stocks Qty reset to 0, same operation as Month-End Rollover just
triggered on the date Accounting picked instead of the 1st of the month.
effective_applied_at records when that fold actually ran, so it only ever
happens once per count.

Revision ID: 0061
Revises: 0060
Create Date: 2026-09-03
"""
from alembic import op
import sqlalchemy as sa

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("stock_counts", sa.Column("effective_date", sa.Date(), nullable=True))
    op.add_column(
        "stock_counts", sa.Column("effective_applied_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("stock_counts", "effective_applied_at")
    op.drop_column("stock_counts", "effective_date")
