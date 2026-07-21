"""Add private edition event state and source-open audit records.

Revision ID: 0013_market_morning_edition_interactions
Revises: 0012_market_morning_delivery_webhooks
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = "0013_market_morning_edition_interactions"
down_revision: str | None = "0012_market_morning_delivery_webhooks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def upgrade() -> None:
    op.create_table(
        "mm_edition_event_states",
        sa.Column("event_state_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("edition_id", sa.String(36), nullable=False),
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("first_read_at", mysql.DATETIME(fsp=6)),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint(
            "state IN ('read', 'later', 'irrelevant')",
            name="ck_mm_edition_event_state",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["mm_users.user_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["edition_id"],
            ["mm_morning_editions.edition_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "user_id",
            "edition_id",
            "event_id",
            name="uq_mm_edition_event_state",
        ),
        **_OPTIONS,
    )
    op.create_index(
        "ix_mm_edition_event_state_user_edition",
        "mm_edition_event_states",
        ["user_id", "edition_id", "state"],
    )
    op.create_table(
        "mm_edition_source_opens",
        sa.Column("source_open_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("edition_id", sa.String(36), nullable=False),
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("source_record_id", sa.String(36), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("opened_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["mm_users.user_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["edition_id"],
            ["mm_morning_editions.edition_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_record_id"],
            ["mm_source_records.source_record_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "user_id",
            "request_id",
            name="uq_mm_edition_source_open_request",
        ),
        **_OPTIONS,
    )
    op.create_index(
        "ix_mm_edition_source_open_source",
        "mm_edition_source_opens",
        ["source_record_id", "opened_at"],
    )
    op.create_index(
        "ix_mm_edition_source_open_user",
        "mm_edition_source_opens",
        ["user_id", "opened_at"],
    )


def downgrade() -> None:
    op.drop_table("mm_edition_source_opens")
    op.drop_table("mm_edition_event_states")
