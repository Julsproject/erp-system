"""stock_count_lines.include_in_beginning — whether a counted item goes into
the Actual Beginning its count sets on the Effective Date. Unticked on the
Counted Items list, that one product is skipped by the effective-date fold;
the count's stock correction itself is untouched. include_changed_at/_by
record who last flipped it, for tracing.

Revision ID: 0078
Revises: 0077
Create Date: 2026-09-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("stock_count_lines",
                  sa.Column("include_in_beginning", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("stock_count_lines", sa.Column("include_changed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("stock_count_lines",
                  sa.Column("include_changed_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True))


def downgrade() -> None:
    op.drop_column("stock_count_lines", "include_changed_by")
    op.drop_column("stock_count_lines", "include_changed_at")
    op.drop_column("stock_count_lines", "include_in_beginning")
