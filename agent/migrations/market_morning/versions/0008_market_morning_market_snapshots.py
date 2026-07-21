"""Create immutable publication-grade market snapshots.

Revision ID: 0008_market_morning_market_snapshots
Revises: 0007_market_morning_manual_overrides
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = "0008_market_morning_market_snapshots"
down_revision: str | None = "0007_market_morning_manual_overrides"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mm_market_snapshots",
        sa.Column("snapshot_id", sa.String(36), primary_key=True),
        sa.Column("instrument", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("as_of", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("value", sa.Numeric(24, 8), nullable=False),
        sa.Column("previous_close", sa.Numeric(24, 8)),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("delay_status", sa.String(16), nullable=False),
        sa.Column("fetched_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.UniqueConstraint(
            "instrument",
            "provider",
            "session_date",
            "as_of",
            name="uq_mm_market_snapshot_identity",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_mm_market_snapshot_latest",
        "mm_market_snapshots",
        ["instrument", "session_date", "as_of"],
    )


def downgrade() -> None:
    op.drop_table("mm_market_snapshots")
