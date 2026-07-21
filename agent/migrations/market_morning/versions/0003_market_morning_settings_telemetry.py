"""Create deletion requests and privacy-safe telemetry.

Revision ID: 0003_market_morning_settings
Revises: 0002_market_morning_watchlist
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = "0003_market_morning_settings"
down_revision: str | None = "0002_market_morning_watchlist"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mm_account_deletion_requests",
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("requested_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("processing_started_at", mysql.DATETIME(fsp=6)),
        sa.Column("completed_at", mysql.DATETIME(fsp=6)),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column(
            "active_user_id",
            sa.String(36),
            sa.Computed(
                "CASE WHEN status IN ('pending', 'processing') THEN user_id ELSE NULL END",
                persisted=True,
            ),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["mm_users.user_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "active_user_id",
            name="uq_mm_account_deletion_active_user",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_mm_account_deletion_status_requested",
        "mm_account_deletion_requests",
        ["status", "requested_at"],
    )

    op.create_table(
        "mm_analytics_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("event_name", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(36)),
        sa.Column("request_id", sa.String(64)),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("properties", mysql.JSON(), nullable=False),
        sa.Column("occurred_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["mm_users.user_id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "event_name",
            "request_id",
            name="uq_mm_analytics_event_request",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_mm_analytics_name_occurred",
        "mm_analytics_events",
        ["event_name", "occurred_at"],
    )
    op.create_index(
        "ix_mm_analytics_user_occurred",
        "mm_analytics_events",
        ["user_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_table("mm_analytics_events")
    op.drop_table("mm_account_deletion_requests")
