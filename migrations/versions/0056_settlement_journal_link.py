"""ReceivableSettlement.journal_entry_id — links a settlement to the exact
journal entry it posted, so an accidental payment can be undone by reversing
that specific entry (source_type/source_id alone is keyed to the sale, which
is ambiguous when a sale has more than one settlement).

Revision ID: 0056
Revises: 0055
Create Date: 2026-08-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("receivable_settlements", sa.Column("journal_entry_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_receivable_settlements_journal_entry_id", "receivable_settlements",
        "journal_entries", ["journal_entry_id"], ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_receivable_settlements_journal_entry_id", "receivable_settlements", type_="foreignkey")
    op.drop_column("receivable_settlements", "journal_entry_id")
