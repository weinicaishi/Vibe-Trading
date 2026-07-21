"""Create immutable per-user morning-edition snapshots.

Revision ID: 0005_market_morning_editions
Revises: 0004_market_morning_sources_events
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = "0005_market_morning_editions"
down_revision: str | None = "0004_market_morning_sources_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def upgrade() -> None:
    op.create_table(
        "mm_morning_editions",
        sa.Column("edition_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("edition_date", sa.Date(), nullable=False),
        sa.Column("edition_version", sa.Integer(), nullable=False),
        sa.Column("generation_key", sa.String(128), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("payload", mysql.JSON(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("generated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("published_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("supersedes_edition_id", sa.String(36)),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["mm_users.user_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_edition_id"],
            ["mm_morning_editions.edition_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "user_id",
            "edition_date",
            "edition_version",
            name="uq_mm_edition_user_date_version",
        ),
        sa.UniqueConstraint(
            "user_id",
            "generation_key",
            name="uq_mm_edition_user_generation_key",
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_mm_edition_user_date_version",
        "mm_morning_editions",
        ["user_id", "edition_date", "edition_version"],
    )


def downgrade() -> None:
    op.drop_table("mm_morning_editions")
