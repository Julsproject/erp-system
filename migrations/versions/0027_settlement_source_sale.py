"""receivable_settlements.source_sale_id — which return/exchange (if any)
produced this settlement, so a customer's statement can show "returned
5 hammers on invoice X" instead of just an unexplained payment.

Revision ID: 0027
Revises: 0026
Create Date: 2026-07-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("receivable_settlements", sa.Column("source_sale_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_receivable_settlements_source_sale_id", "receivable_settlements",
        "sales", ["source_sale_id"], ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_receivable_settlements_source_sale_id", "receivable_settlements", type_="foreignkey")
    op.drop_column("receivable_settlements", "source_sale_id")
