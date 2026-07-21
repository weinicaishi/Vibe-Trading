"""Create versioned EventBrief generation and source manifests.

Revision ID: 0010_market_morning_event_briefs
Revises: 0009_market_morning_global_runs
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = "0010_market_morning_event_briefs"
down_revision: str | None = "0009_market_morning_global_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def upgrade() -> None:
    op.create_table(
        "mm_event_briefs",
        sa.Column("brief_id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False),
        sa.Column("generation_key", sa.String(191), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("review_status", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("spec", mysql.JSON(), nullable=False),
        sa.Column("spec_sha256", sa.String(64), nullable=False),
        sa.Column("payload", mysql.JSON()),
        sa.Column("payload_sha256", sa.String(64)),
        sa.Column("source_ids", mysql.JSON(), nullable=False),
        sa.Column("validation_errors", mysql.JSON(), nullable=False),
        sa.Column("primary_source_record_id", sa.String(36)),
        sa.Column("last_failure_code", sa.String(64)),
        sa.Column("last_failed_at", mysql.DATETIME(fsp=6)),
        sa.Column("started_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("completed_at", mysql.DATETIME(fsp=6)),
        sa.Column("published_at", mysql.DATETIME(fsp=6)),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint(
            "status NOT IN ('published', 'degraded') OR "
            "(primary_source_record_id IS NOT NULL AND JSON_LENGTH(source_ids) > 0)",
            name="ck_mm_event_brief_publishable_source",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"], ["mm_normalized_events.event_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["primary_source_record_id"],
            ["mm_source_records.source_record_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "generation_key", name="uq_mm_event_brief_generation_key"
        ),
        sa.UniqueConstraint(
            "event_id",
            "event_version",
            "schema_version",
            "model_version",
            "prompt_version",
            name="uq_mm_event_brief_generation_spec",
        ),
        **_OPTIONS,
    )
    op.create_index(
        "ix_mm_event_brief_event_status",
        "mm_event_briefs",
        ["event_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_mm_event_brief_review",
        "mm_event_briefs",
        ["review_status", "published_at"],
    )
    op.create_table(
        "mm_event_brief_sources",
        sa.Column("brief_source_id", sa.String(36), primary_key=True),
        sa.Column("brief_id", sa.String(36), nullable=False),
        sa.Column("source_record_id", sa.String(36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["brief_id"], ["mm_event_briefs.brief_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_record_id"],
            ["mm_source_records.source_record_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "brief_id",
            "source_record_id",
            name="uq_mm_event_brief_source_link",
        ),
        sa.UniqueConstraint(
            "brief_id",
            "position",
            name="uq_mm_event_brief_source_position",
        ),
        **_OPTIONS,
    )
    op.create_index(
        "ix_mm_event_brief_source_record",
        "mm_event_brief_sources",
        ["source_record_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("mm_event_brief_sources")
    op.drop_table("mm_event_briefs")
