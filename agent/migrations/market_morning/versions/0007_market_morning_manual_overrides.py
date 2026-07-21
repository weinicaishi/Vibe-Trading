"""Create audited publication halt overrides.

Revision ID: 0007_market_morning_manual_overrides
Revises: 0006_market_morning_jobs
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = "0007_market_morning_manual_overrides"
down_revision: str | None = "0006_market_morning_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mm_manual_overrides",
        sa.Column("override_id", sa.String(36), primary_key=True),
        sa.Column("override_type", sa.String(32), nullable=False),
        sa.Column("edition_date", sa.Date(), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "active_edition_date",
            sa.Date(),
            sa.Computed(
                "CASE WHEN override_type = 'publication_halt' AND status = 'active' THEN edition_date ELSE NULL END",
                persisted=True,
            ),
        ),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("revoked_by", sa.String(128)),
        sa.Column("revoked_at", mysql.DATETIME(fsp=6)),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.UniqueConstraint(
            "active_edition_date",
            name="uq_mm_manual_override_active_date",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_mm_manual_override_date_status",
        "mm_manual_overrides",
        ["edition_date", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("mm_manual_overrides")
