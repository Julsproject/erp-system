"""Customer credit balance: an overpayment on Collect Payment, or a paid
invoice whose total was corrected down, is kept as the customer's advance
(2050 Customer Deposits / Advances, a liability) instead of being lost or
booked as sales — used on their next collection or refunded.

Revision ID: 0084
Revises: 0083
Create Date: 2026-10-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    acct_id = conn.execute(sa.text("SELECT id FROM accounts WHERE system_key = 'CUSTOMER_DEPOSITS' OR code = '2050'")).scalar()
    if not acct_id:
        acct_id = conn.execute(sa.text(
            "INSERT INTO accounts (code, name, account_type, normal_balance, is_system, system_key, is_active) "
            "VALUES ('2050', 'Customer Deposits / Advances', 'liability', 'credit', true, 'CUSTOMER_DEPOSITS', true) RETURNING id"
        )).scalar()
    if not conn.execute(sa.text("SELECT 1 FROM account_mappings WHERE function_key = 'CUSTOMER_CREDIT'")).scalar():
        conn.execute(sa.text(
            "INSERT INTO account_mappings (function_key, label, account_id) VALUES "
            "('CUSTOMER_CREDIT', 'Customer credit balance — overpayments / advances (liability)', :a)"
        ), {"a": acct_id})

    op.create_table(
        "customer_credits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False, index=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("method", sa.String(20), nullable=True),
        sa.Column("ref_no", sa.String(60), nullable=True),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("sale_id", sa.Integer(), sa.ForeignKey("sales.id"), nullable=True),
        sa.Column("settlement_id", sa.Integer(), sa.ForeignKey("receivable_settlements.id", ondelete="CASCADE"), nullable=True),
        sa.Column("journal_entry_id", sa.Integer(), sa.ForeignKey("journal_entries.id"), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("customer_credits")
    op.execute("DELETE FROM account_mappings WHERE function_key = 'CUSTOMER_CREDIT'")
