"""audit_log — system-wide who-did-what trail

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("username", sa.String(length=50)),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("entity_type", sa.String(length=30), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("entity_label", sa.String(length=150)),
        sa.Column("summary", sa.String(length=300)),
        sa.Column("changes", sa.Text()),
        sa.Column("ip", sa.String(length=45)),
    )
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_entity_type", "audit_log", ["entity_type"])


def downgrade() -> None:
    op.drop_table("audit_log")
