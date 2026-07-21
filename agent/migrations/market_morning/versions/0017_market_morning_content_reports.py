"""Add privacy-safe user content reports and operator resolution state.

Revision ID: 0017_market_morning_content_reports
Revises: 0016_market_morning_model_usage
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = "0017_market_morning_content_reports"
down_revision: str | None = "0016_market_morning_model_usage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def upgrade() -> None:
    # This revision ID is longer than Alembic's default VARCHAR(32). Widen the
    # version table before Alembic records the new head. Keep VARCHAR(64) on
    # downgrade so the current 0017 value can still be replaced safely.
    op.alter_column(
        "alembic_version",
        "version_num",
        existing_type=sa.String(32),
        type_=sa.String(64),
        existing_nullable=False,
    )
    op.create_table(
        "mm_content_reports",
        sa.Column("report_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("edition_id", sa.String(36), nullable=False),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("reason_code", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("resolution_code", sa.String(64)),
        sa.Column("reviewed_by", sa.String(128)),
        sa.Column("reviewed_at", mysql.DATETIME(fsp=6)),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint(
            "reason_code IN ('fact_inaccurate', 'source_mismatch', "
            "'outdated_or_corrected', 'other_content_issue')",
            name="ck_mm_content_report_reason",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'resolved', 'dismissed')",
            name="ck_mm_content_report_status",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND resolution_code IS NULL "
            "AND reviewed_by IS NULL AND reviewed_at IS NULL) OR "
            "(status IN ('resolved', 'dismissed') AND resolution_code IS NOT NULL "
            "AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="ck_mm_content_report_resolution_state",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["mm_users.user_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["edition_id"],
            ["mm_morning_editions.edition_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["mm_normalized_events.event_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "user_id",
            "edition_id",
            "event_id",
            name="uq_mm_content_report_user_edition_event",
        ),
        **_OPTIONS,
    )
    op.create_index(
        "ix_mm_content_report_status_created",
        "mm_content_reports",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_mm_content_report_event_created",
        "mm_content_reports",
        ["event_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("mm_content_reports")
