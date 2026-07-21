"""Add private-beta invitation lifecycle storage.

Revision ID: 0015_market_morning_beta_privacy
Revises: 0014_market_morning_issuer_research
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = "0015_market_morning_beta_privacy"
down_revision: str | None = "0014_market_morning_issuer_research"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def upgrade() -> None:
    op.create_table(
        "mm_private_beta_invites",
        sa.Column("invite_id", sa.String(36), primary_key=True),
        sa.Column("token_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("expires_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("created_by_reference", sa.String(255), nullable=False),
        sa.Column("accepted_by_user_id", sa.String(36)),
        sa.Column("accepted_at", mysql.DATETIME(fsp=6)),
        sa.Column("revoked_at", mysql.DATETIME(fsp=6)),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'revoked', 'expired')",
            name="ck_mm_private_beta_invite_status",
        ),
        sa.ForeignKeyConstraint(
            ["accepted_by_user_id"], ["mm_users.user_id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "token_sha256",
            name="uq_mm_private_beta_invite_token_sha256",
        ),
        **_OPTIONS,
    )
    op.create_index(
        "ix_mm_private_beta_invite_status_expires",
        "mm_private_beta_invites",
        ["status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_table("mm_private_beta_invites")
