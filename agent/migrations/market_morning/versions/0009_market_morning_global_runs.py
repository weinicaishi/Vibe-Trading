"""Create serialized global edition runs and snapshot manifests.

Revision ID: 0009_market_morning_global_runs
Revises: 0008_market_morning_market_snapshots
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = "0009_market_morning_global_runs"
down_revision: str | None = "0008_market_morning_market_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def upgrade() -> None:
    op.create_table(
        "mm_global_edition_days",
        sa.Column("edition_date", sa.Date(), primary_key=True),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        **_OPTIONS,
    )
    op.create_table(
        "mm_global_edition_runs",
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("edition_date", sa.Date(), nullable=False),
        sa.Column("run_version", sa.Integer(), nullable=False),
        sa.Column("generation_key", sa.String(191), nullable=False),
        sa.Column("attempt_key", sa.String(16), nullable=False),
        sa.Column("scenario", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column(
            "current_success_date",
            sa.Date(),
            sa.Computed(
                "CASE WHEN is_current = 1 AND status IN ('complete', 'partial', 'late') THEN edition_date ELSE NULL END",
                persisted=True,
            ),
        ),
        sa.Column("email_permitted", sa.Boolean(), nullable=False),
        sa.Column("late", sa.Boolean(), nullable=False),
        sa.Column("reason_code", sa.String(64)),
        sa.Column("spec", mysql.JSON(), nullable=False),
        sa.Column("spec_sha256", sa.String(64), nullable=False),
        sa.Column("manifest", mysql.JSON()),
        sa.Column("manifest_sha256", sa.String(64)),
        sa.Column("started_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("completed_at", mysql.DATETIME(fsp=6)),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["edition_date"],
            ["mm_global_edition_days.edition_date"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "edition_date", "run_version", name="uq_mm_global_run_date_version"
        ),
        sa.UniqueConstraint(
            "generation_key", name="uq_mm_global_run_generation_key"
        ),
        sa.UniqueConstraint(
            "current_success_date", name="uq_mm_global_run_current_success"
        ),
        **_OPTIONS,
    )
    op.create_index(
        "ix_mm_global_run_date_status",
        "mm_global_edition_runs",
        ["edition_date", "status", "started_at"],
    )
    op.create_table(
        "mm_global_edition_items",
        sa.Column("item_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("item_type", sa.String(32), nullable=False),
        sa.Column("snapshot_id", sa.String(36), nullable=False),
        sa.Column("instrument", sa.String(32), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["mm_global_edition_runs.run_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["mm_market_snapshots.snapshot_id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "run_id", "position", name="uq_mm_global_item_run_position"
        ),
        sa.UniqueConstraint(
            "run_id", "snapshot_id", name="uq_mm_global_item_run_snapshot"
        ),
        **_OPTIONS,
    )
    op.create_index(
        "ix_mm_global_item_run_type",
        "mm_global_edition_items",
        ["run_id", "item_type", "position"],
    )


def downgrade() -> None:
    op.drop_table("mm_global_edition_items")
    op.drop_table("mm_global_edition_runs")
    op.drop_table("mm_global_edition_days")
