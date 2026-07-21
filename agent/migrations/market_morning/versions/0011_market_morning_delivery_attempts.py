"""Create privacy-safe idempotent email delivery attempts.

Revision ID: 0011_market_morning_delivery_attempts
Revises: 0010_market_morning_event_briefs
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = "0011_market_morning_delivery_attempts"
down_revision: str | None = "0010_market_morning_event_briefs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def upgrade() -> None:
    op.create_table(
        "mm_delivery_attempts",
        sa.Column("delivery_attempt_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("global_run_id", sa.String(36), nullable=False),
        sa.Column("edition_date", sa.Date(), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(191), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("provider_message_id", sa.String(191)),
        sa.Column("deep_link_token_sha256", sa.String(64), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("requested_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("sent_at", mysql.DATETIME(fsp=6)),
        sa.Column("delivered_at", mysql.DATETIME(fsp=6)),
        sa.Column("clicked_at", mysql.DATETIME(fsp=6)),
        sa.Column("failed_at", mysql.DATETIME(fsp=6)),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'delivered', "
            "'failed', 'clicked', 'suppressed')",
            name="ck_mm_delivery_status",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["mm_users.user_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["global_run_id"],
            ["mm_global_edition_runs.run_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "user_id",
            "edition_date",
            "channel",
            name="uq_mm_delivery_user_date_channel",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_mm_delivery_idempotency_key",
        ),
        sa.UniqueConstraint(
            "provider_message_id",
            name="uq_mm_delivery_provider_message",
        ),
        **_OPTIONS,
    )
    op.create_index(
        "ix_mm_delivery_status_requested",
        "mm_delivery_attempts",
        ["status", "requested_at"],
    )
    op.create_index(
        "ix_mm_delivery_user_date",
        "mm_delivery_attempts",
        ["user_id", "edition_date", "requested_at"],
    )


def downgrade() -> None:
    op.drop_table("mm_delivery_attempts")
