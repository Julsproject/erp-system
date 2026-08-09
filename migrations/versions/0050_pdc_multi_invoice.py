"""Cash & Banking Phase 4: let one cheque apply against multiple invoices.
New pdc_applications table (pdc_id, sale_id/purchase_id, amount) replaces
the single sale_id/purchase_id-per-PDC shape going forward. Existing
PostDatedCheque rows keep their sale_id/purchase_id columns (untouched,
now legacy-only) and get exactly one backfilled application row each so
every PDC has >=1 application from here on, uniformly.

Revision ID: 0050
Revises: 0049
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pdc_applications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pdc_id", sa.Integer(), sa.ForeignKey("post_dated_cheques.id"), nullable=False),
        sa.Column("sale_id", sa.Integer(), sa.ForeignKey("sales.id"), nullable=True),
        sa.Column("purchase_id", sa.Integer(), sa.ForeignKey("purchases.id"), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
    )
    op.create_index("ix_pdc_applications_pdc_id", "pdc_applications", ["pdc_id"])

    conn = op.get_bind()
    conn.execute(sa.text(
        """
        INSERT INTO pdc_applications (pdc_id, sale_id, purchase_id, amount)
        SELECT id, sale_id, purchase_id, amount FROM post_dated_cheques
        WHERE sale_id IS NOT NULL OR purchase_id IS NOT NULL
        """
    ))


def downgrade() -> None:
    op.drop_index("ix_pdc_applications_pdc_id", table_name="pdc_applications")
    op.drop_table("pdc_applications")
