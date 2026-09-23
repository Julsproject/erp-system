"""inventory_adjustment_lines.is_move — the line is one side of a "Move
between items" (stock recorded on the wrong item, e.g. a box sitting on
the Open/Retail item). A move is a reclassification, so its lines always
post as a correction (equity, off the P&L) whatever the adjustment's reason.

Revision ID: 0077
Revises: 0076
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("inventory_adjustment_lines",
                  sa.Column("is_move", sa.Boolean(), nullable=False, server_default="false"))


def downgrade() -> None:
    op.drop_column("inventory_adjustment_lines", "is_move")
