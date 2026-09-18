"""Links that make un-clearing a cheque possible.

purchase_settlements.journal_entry_id — the AP mirror of the column
receivable_settlements already got in 0056: the exact entry a settlement
posted, so it can be reversed on its own when a purchase has several.

*_settlements.pdc_id — which cheque's clearing produced the settlement.
Undoing a cleared cheque has to know exactly which settlements it created;
matching on cheque_no would be a guess, since numbers repeat across banks.

Revision ID: 0070
Revises: 0069
Create Date: 2026-09-18
"""
from alembic import op
import sqlalchemy as sa

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("purchase_settlements") as batch:
        batch.add_column(sa.Column("journal_entry_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("pdc_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_purchase_settlements_journal_entry_id", "journal_entries",
            ["journal_entry_id"], ["id"])
        batch.create_foreign_key(
            "fk_purchase_settlements_pdc_id", "post_dated_cheques", ["pdc_id"], ["id"])

    with op.batch_alter_table("receivable_settlements") as batch:
        batch.add_column(sa.Column("pdc_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_receivable_settlements_pdc_id", "post_dated_cheques", ["pdc_id"], ["id"])

    op.create_index("ix_purchase_settlements_pdc_id", "purchase_settlements", ["pdc_id"])
    op.create_index("ix_receivable_settlements_pdc_id", "receivable_settlements", ["pdc_id"])


def downgrade() -> None:
    op.drop_index("ix_receivable_settlements_pdc_id", table_name="receivable_settlements")
    op.drop_index("ix_purchase_settlements_pdc_id", table_name="purchase_settlements")
    with op.batch_alter_table("receivable_settlements") as batch:
        batch.drop_constraint("fk_receivable_settlements_pdc_id", type_="foreignkey")
        batch.drop_column("pdc_id")
    with op.batch_alter_table("purchase_settlements") as batch:
        batch.drop_constraint("fk_purchase_settlements_pdc_id", type_="foreignkey")
        batch.drop_constraint("fk_purchase_settlements_journal_entry_id", type_="foreignkey")
        batch.drop_column("pdc_id")
        batch.drop_column("journal_entry_id")
