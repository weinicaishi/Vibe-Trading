"""Create hash-only idempotent delivery provider webhook events.

Revision ID: 0012_market_morning_delivery_webhooks
Revises: 0011_market_morning_delivery_attempts
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = "0012_market_morning_delivery_webhooks"
down_revision: str | None = "0011_market_morning_delivery_attempts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def upgrade() -> None:
    op.create_table(
        "mm_delivery_provider_events",
        sa.Column("provider_event_row_id", sa.String(36), primary_key=True),
        sa.Column("delivery_attempt_id", sa.String(36)),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("provider_event_id", sa.String(191), nullable=False),
        sa.Column("provider_message_id", sa.String(191), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("received_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("processed_at", mysql.DATETIME(fsp=6)),
        sa.CheckConstraint(
            "event_type IN ('delivered', 'failed', 'clicked')",
            name="ck_mm_delivery_provider_event_type",
        ),
        sa.CheckConstraint(
            "outcome IN ('pending', 'applied', 'stale', 'unmatched', 'rejected')",
            name="ck_mm_delivery_provider_event_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["delivery_attempt_id"],
            ["mm_delivery_attempts.delivery_attempt_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "provider",
            "provider_event_id",
            name="uq_mm_delivery_provider_event",
        ),
        **_OPTIONS,
    )
    op.create_index(
        "ix_mm_delivery_provider_message_received",
        "mm_delivery_provider_events",
        ["provider_message_id", "received_at"],
    )
    op.create_index(
        "ix_mm_delivery_provider_outcome",
        "mm_delivery_provider_events",
        ["outcome", "received_at"],
    )


def downgrade() -> None:
    op.drop_table("mm_delivery_provider_events")
