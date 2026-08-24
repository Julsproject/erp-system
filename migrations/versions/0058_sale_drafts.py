"""Parked POS sales — "save as draft", pick up later.

A cashier halfway through a long list hits something they can't resolve yet
(an item not on file, a price to confirm) and needs the till free for the
next customer. Without this the only options are re-typing everything later
or holding up the queue.

Deliberately separate from Quotation: a quotation is a customer-facing
document with its own number, statuses and PDF. This is internal scratch —
no number, never printed, and it touches nothing until it's resumed and
completed as an ordinary sale.

The cart is stored as a JSON blob rather than normalised rows on purpose: it
holds live client-side POS state (unit ladder, chosen unit index, per-line
discount type, custom-price flags) that only means anything when loaded back
into the same screen, and a half-finished scratch entry is not something
reports should ever read.

Revision ID: 0058
Revises: 0057
Create Date: 2026-08-21
"""
from alembic import op
import sqlalchemy as sa

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sale_drafts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(120), nullable=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_sale_drafts_created_by", "sale_drafts", ["created_by"])


def downgrade() -> None:
    op.drop_index("ix_sale_drafts_created_by", table_name="sale_drafts")
    op.drop_table("sale_drafts")
