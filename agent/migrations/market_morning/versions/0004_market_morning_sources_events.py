"""Create versioned official-source and normalized-event storage.

Revision ID: 0004_market_morning_sources_events
Revises: 0003_market_morning_settings
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = "0004_market_morning_sources_events"
down_revision: str | None = "0003_market_morning_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def upgrade() -> None:
    op.create_table(
        "mm_source_cursors",
        sa.Column("source_provider", sa.String(64), primary_key=True),
        sa.Column("cursor_value", mysql.JSON()),
        sa.Column("last_successful_discovery_at", mysql.DATETIME(fsp=6)),
        sa.Column("last_error_at", mysql.DATETIME(fsp=6)),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_mm_source_cursor_health",
        "mm_source_cursors",
        ["last_error_at", "last_successful_discovery_at"],
    )

    op.create_table(
        "mm_source_records",
        sa.Column("source_record_id", sa.String(36), primary_key=True),
        sa.Column("source_provider", sa.String(64), nullable=False),
        sa.Column("provider_document_id", sa.String(191), nullable=False),
        sa.Column("provider_revision_key", sa.String(128), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("document_type", sa.String(64), nullable=False),
        sa.Column("published_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("fetched_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("lifecycle_status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("content_hash_sha256", sa.String(64), nullable=False),
        sa.Column("normalized_payload", mysql.JSON(), nullable=False),
        sa.Column("supersedes_record_id", sa.String(36)),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["supersedes_record_id"],
            ["mm_source_records.source_record_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "source_provider",
            "provider_document_id",
            "provider_revision_key",
            name="uq_mm_source_record_provider_document_revision",
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_mm_source_record_document_history",
        "mm_source_records",
        ["source_provider", "provider_document_id", "published_at"],
    )
    op.create_index(
        "ix_mm_source_record_status_published",
        "mm_source_records",
        ["lifecycle_status", "published_at"],
    )

    op.create_table(
        "mm_normalized_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column(
            "event_family_key",
            sa.String(191, collation="utf8mb4_bin"),
            nullable=False,
        ),
        sa.Column("event_version", sa.Integer(), nullable=False),
        sa.Column("issuer_id", sa.String(36), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column(
            "normalized_title",
            sa.String(512, collation="utf8mb4_bin"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("occurred_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("lifecycle_status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("supersedes_event_id", sa.String(36)),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(["issuer_id"], ["mm_issuers.issuer_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["supersedes_event_id"],
            ["mm_normalized_events.event_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "event_family_key", "event_version", name="uq_mm_event_family_version"
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_mm_event_issuer_occurred",
        "mm_normalized_events",
        ["issuer_id", "occurred_at"],
    )
    op.create_index(
        "ix_mm_event_status_occurred",
        "mm_normalized_events",
        ["lifecycle_status", "occurred_at"],
    )

    op.create_table(
        "mm_event_sources",
        sa.Column("event_source_id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("source_record_id", sa.String(36), nullable=False),
        sa.Column("relation_type", sa.String(32), nullable=False, server_default="primary"),
        sa.Column("linked_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"], ["mm_normalized_events.event_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_record_id"], ["mm_source_records.source_record_id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("event_id", "source_record_id", name="uq_mm_event_source_link"),
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_mm_event_source_record",
        "mm_event_sources",
        ["source_record_id", "linked_at"],
    )

    op.create_table(
        "mm_event_merge_candidates",
        sa.Column("candidate_id", sa.String(36), primary_key=True),
        sa.Column("left_event_id", sa.String(36), nullable=False),
        sa.Column("right_event_id", sa.String(36), nullable=False),
        sa.Column("reason", sa.String(128), nullable=False),
        sa.Column("title_similarity", sa.Float(), nullable=False),
        sa.Column("time_distance_seconds", sa.Integer(), nullable=False),
        sa.Column("review_status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("reviewed_by", sa.String(255)),
        sa.Column("reviewed_at", mysql.DATETIME(fsp=6)),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["left_event_id"], ["mm_normalized_events.event_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["right_event_id"], ["mm_normalized_events.event_id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "left_event_id", "right_event_id", name="uq_mm_event_merge_candidate_pair"
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_mm_event_merge_review",
        "mm_event_merge_candidates",
        ["review_status", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("mm_event_merge_candidates")
    op.drop_table("mm_event_sources")
    op.drop_table("mm_normalized_events")
    op.drop_table("mm_source_records")
    op.drop_table("mm_source_cursors")
