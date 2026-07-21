"""Create the Market Morning user watchlist.

Revision ID: 0002_market_morning_watchlist
Revises: 0001_market_morning_foundation
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = "0002_market_morning_watchlist"
down_revision: str | None = "0001_market_morning_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mm_watchlist_items",
        sa.Column("watchlist_item_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("issuer_id", sa.String(36), nullable=False),
        sa.Column("user_label", sa.String(40)),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("removed_at", mysql.DATETIME(fsp=6)),
        sa.Column(
            "active_issuer_id",
            sa.String(36),
            sa.Computed(
                "CASE WHEN removed_at IS NULL THEN issuer_id ELSE NULL END",
                persisted=True,
            ),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["mm_users.user_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["issuer_id"], ["mm_issuers.issuer_id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "user_id",
            "active_issuer_id",
            name="uq_mm_watchlist_active_user_issuer",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_mm_watchlist_user_active_order",
        "mm_watchlist_items",
        ["user_id", "removed_at", "sort_order", "created_at"],
    )
    op.create_index(
        "ix_mm_watchlist_issuer_active",
        "mm_watchlist_items",
        ["issuer_id", "removed_at"],
    )


def downgrade() -> None:
    op.drop_table("mm_watchlist_items")
