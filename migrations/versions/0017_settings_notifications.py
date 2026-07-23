"""settings UI (app_settings) and Notifications Center (notifications)

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=60), primary_key=True),
        sa.Column("value", sa.String(length=500)),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dedupe_key", sa.String(length=80), nullable=False),
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("severity", sa.String(length=10), server_default="info", nullable=False),
        sa.Column("title", sa.String(length=150), nullable=False),
        sa.Column("body", sa.String(length=300)),
        sa.Column("link", sa.String(length=120)),
        sa.Column("is_read", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("is_resolved", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_notifications_dedupe_key", "notifications", ["dedupe_key"])


def downgrade() -> None:
    op.drop_table("notifications")
    op.drop_table("app_settings")
