"""cheque_books + cheque series/bank-account columns on post_dated_cheques.

Makes an issued cheque belong to a numbered booklet drawn on one of our own
bank accounts, so the numbers can be monitored as a series (next number,
duplicate guard, gap detection) instead of being free text. Also adds the
link to the bank withdrawal a cheque causes when it clears, so the account
balance finally drops on the day the money actually left.

Revision ID: 0069
Revises: 0068
Create Date: 2026-09-18
"""
from alembic import op
import sqlalchemy as sa

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cheque_books",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("bank_account_id", sa.Integer(), sa.ForeignKey("bank_accounts.id"), nullable=False),
        sa.Column("prefix", sa.String(20)),
        sa.Column("start_no", sa.Integer(), nullable=False),
        sa.Column("end_no", sa.Integer(), nullable=False),
        sa.Column("digits", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("notes", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_cheque_books_bank_account_id", "cheque_books", ["bank_account_id"])

    with op.batch_alter_table("post_dated_cheques") as batch:
        batch.add_column(sa.Column("bank_account_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("cheque_book_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("cheque_seq", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("bank_txn_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_pdc_bank_account_id", "bank_accounts", ["bank_account_id"], ["id"])
        batch.create_foreign_key(
            "fk_pdc_cheque_book_id", "cheque_books", ["cheque_book_id"], ["id"])
        batch.create_foreign_key(
            "fk_pdc_bank_txn_id", "bank_transactions", ["bank_txn_id"], ["id"])

    # One number can't be issued twice on the same booklet. Partial so the
    # pile of existing cheques (and every received one) — all NULL on both
    # columns — stays legal.
    op.create_index(
        "ux_pdc_book_seq", "post_dated_cheques", ["cheque_book_id", "cheque_seq"],
        unique=True, sqlite_where=sa.text("cheque_book_id IS NOT NULL"),
        postgresql_where=sa.text("cheque_book_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_pdc_book_seq", table_name="post_dated_cheques")
    with op.batch_alter_table("post_dated_cheques") as batch:
        batch.drop_constraint("fk_pdc_bank_txn_id", type_="foreignkey")
        batch.drop_constraint("fk_pdc_cheque_book_id", type_="foreignkey")
        batch.drop_constraint("fk_pdc_bank_account_id", type_="foreignkey")
        batch.drop_column("bank_txn_id")
        batch.drop_column("cheque_seq")
        batch.drop_column("cheque_book_id")
        batch.drop_column("bank_account_id")
    op.drop_index("ix_cheque_books_bank_account_id", table_name="cheque_books")
    op.drop_table("cheque_books")
