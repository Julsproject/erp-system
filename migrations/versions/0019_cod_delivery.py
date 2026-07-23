"""cash on delivery: collect a sale's balance when the delivery is handed over

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deliveries", sa.Column("is_cod", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("deliveries", sa.Column("cod_amount", sa.Numeric(12, 2), server_default="0", nullable=False))
    op.add_column("deliveries", sa.Column("collected_amount", sa.Numeric(12, 2), server_default="0", nullable=False))
    op.add_column("deliveries", sa.Column("collected_method", sa.String(length=20), nullable=True))
    op.add_column("deliveries", sa.Column("collected_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("deliveries", sa.Column("settlement_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_deliveries_settlement_id", "deliveries",
        "receivable_settlements", ["settlement_id"], ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_deliveries_settlement_id", "deliveries", type_="foreignkey")
    op.drop_column("deliveries", "settlement_id")
    op.drop_column("deliveries", "collected_at")
    op.drop_column("deliveries", "collected_method")
    op.drop_column("deliveries", "collected_amount")
    op.drop_column("deliveries", "cod_amount")
    op.drop_column("deliveries", "is_cod")
