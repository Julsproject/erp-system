"""stock_movements.journal_entry_id — the journal entry that booked this
movement's value, for movements the books pick up after the fact (the
historical stock counts / manual adjustments folded into the ledger when
the opening inventory was set up). Set means "already in the books" so
nothing posts it twice.

Revision ID: 0073
Revises: 0072
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("stock_movements") as batch:
        batch.add_column(sa.Column("journal_entry_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_stock_movements_journal_entry_id", "journal_entries", ["journal_entry_id"], ["id"])
    op.create_index("ix_stock_movements_journal_entry_id", "stock_movements", ["journal_entry_id"])


def downgrade() -> None:
    op.drop_index("ix_stock_movements_journal_entry_id", table_name="stock_movements")
    with op.batch_alter_table("stock_movements") as batch:
        batch.drop_constraint("fk_stock_movements_journal_entry_id", type_="foreignkey")
        batch.drop_column("journal_entry_id")
