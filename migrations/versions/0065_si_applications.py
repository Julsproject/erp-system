"""si_applications — which DR(s) a consolidated SI documents.

An SI issued later (at collection) to formally recognize Output VAT on one
or more Delivery Receipt sales — a DR is never allowed to carry VAT itself,
see accounting.post_si_conversion. Same shape as pdc_applications (one
document applying to several sales, each with its own covered amount).

Revision ID: 0065
Revises: 0064
Create Date: 2026-09-11
"""
from alembic import op
import sqlalchemy as sa

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "si_applications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("si_sale_id", sa.Integer(), sa.ForeignKey("sales.id"), nullable=False),
        sa.Column("dr_sale_id", sa.Integer(), sa.ForeignKey("sales.id"), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
    )
    op.create_index("ix_si_applications_si_sale_id", "si_applications", ["si_sale_id"])
    op.create_index("ix_si_applications_dr_sale_id", "si_applications", ["dr_sale_id"])


def downgrade() -> None:
    op.drop_index("ix_si_applications_dr_sale_id", table_name="si_applications")
    op.drop_index("ix_si_applications_si_sale_id", table_name="si_applications")
    op.drop_table("si_applications")
