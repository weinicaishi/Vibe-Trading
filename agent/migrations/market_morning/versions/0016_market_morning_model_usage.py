"""Add privacy-safe model usage and cost events.

Revision ID: 0016_market_morning_model_usage
Revises: 0015_market_morning_beta_privacy
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = "0016_market_morning_model_usage"
down_revision: str | None = "0015_market_morning_beta_privacy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def upgrade() -> None:
    op.create_table(
        "mm_model_usage_events",
        sa.Column("usage_event_id", sa.String(36), primary_key=True),
        sa.Column("usage_key", sa.String(191), nullable=False),
        sa.Column("brief_id", sa.String(36), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("usage_status", sa.String(32), nullable=False),
        sa.Column("cost_status", sa.String(32), nullable=False),
        sa.Column("input_tokens", sa.BigInteger()),
        sa.Column("output_tokens", sa.BigInteger()),
        sa.Column("total_tokens", sa.BigInteger()),
        sa.Column("billable_cost_micros", sa.BigInteger()),
        sa.Column("currency", sa.String(3)),
        sa.Column("provider_request_id_sha256", sa.String(64)),
        sa.Column("occurred_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint(
            "attempt_number >= 1",
            name="ck_mm_model_usage_attempt_positive",
        ),
        sa.CheckConstraint(
            "(input_tokens IS NULL OR input_tokens >= 0) "
            "AND (output_tokens IS NULL OR output_tokens >= 0) "
            "AND (total_tokens IS NULL OR total_tokens >= 0) "
            "AND (billable_cost_micros IS NULL OR billable_cost_micros >= 0)",
            name="ck_mm_model_usage_non_negative",
        ),
        sa.CheckConstraint(
            "(usage_status = 'reported' AND input_tokens IS NOT NULL "
            "AND output_tokens IS NOT NULL AND total_tokens = input_tokens + output_tokens) "
            "OR (usage_status = 'missing' AND input_tokens IS NULL "
            "AND output_tokens IS NULL AND total_tokens IS NULL)",
            name="ck_mm_model_usage_token_state",
        ),
        sa.CheckConstraint(
            "(cost_status = 'reported' AND billable_cost_micros IS NOT NULL "
            "AND currency IS NOT NULL) OR (cost_status = 'unpriced' "
            "AND billable_cost_micros IS NULL AND currency IS NULL)",
            name="ck_mm_model_usage_cost_state",
        ),
        sa.ForeignKeyConstraint(
            ["brief_id"], ["mm_event_briefs.brief_id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("usage_key", name="uq_mm_model_usage_key"),
        sa.UniqueConstraint(
            "brief_id",
            "attempt_number",
            name="uq_mm_model_usage_brief_attempt",
        ),
        **_OPTIONS,
    )
    op.create_index(
        "ix_mm_model_usage_occurred_status",
        "mm_model_usage_events",
        ["occurred_at", "usage_status", "cost_status"],
    )


def downgrade() -> None:
    op.drop_table("mm_model_usage_events")
