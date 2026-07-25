"""Accounts Payable: structured supplier payment terms + purchase due_date

Revision ID: 0024
Revises: 0023
Create Date: 2026-07-25
"""
import re

from alembic import op
import sqlalchemy as sa

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("suppliers", sa.Column("payment_days", sa.Integer(), server_default="30", nullable=False))
    op.add_column("purchases", sa.Column("due_date", sa.Date(), nullable=True))

    # Backfill payment_days from the existing free-text payment_terms where a
    # number is readable ("30 days" -> 30), and 0 for COD (due immediately).
    # Anything else (blank, "50% DP", "Consignment") keeps the 30-day default.
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, payment_terms FROM suppliers WHERE payment_terms IS NOT NULL")).fetchall()
    for row_id, terms in rows:
        text = (terms or "").strip().lower()
        if not text:
            continue
        if "cod" in text:
            conn.execute(sa.text("UPDATE suppliers SET payment_days = 0 WHERE id = :id"), {"id": row_id})
            continue
        m = re.search(r"\d+", text)
        if m:
            conn.execute(sa.text("UPDATE suppliers SET payment_days = :days WHERE id = :id"), {"days": int(m.group()), "id": row_id})

    # Backfill due_date for purchases already confirmed/paid, so existing
    # payables show a real (if approximate) due date instead of none.
    conn.execute(sa.text("""
        UPDATE purchases p
        SET due_date = (p.confirmed_at::date + (COALESCE(s.payment_days, 30) || ' days')::interval)::date
        FROM suppliers s
        WHERE p.supplier_id = s.id
          AND p.txn_type = 'receive'
          AND p.status IN ('confirmed', 'paid')
          AND p.confirmed_at IS NOT NULL
    """))


def downgrade() -> None:
    op.drop_column("purchases", "due_date")
    op.drop_column("suppliers", "payment_days")
