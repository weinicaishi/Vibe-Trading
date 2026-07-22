"""Add hash-only application sessions for immediate OIDC revocation.

Revision ID: 0018_market_morning_auth_sessions
Revises: 0017_market_morning_content_reports
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = "0018_market_morning_auth_sessions"
down_revision: str | None = "0017_market_morning_content_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def upgrade() -> None:
    op.create_table(
        "mm_auth_sessions",
        sa.Column("auth_session_id", sa.String(36), primary_key=True),
        sa.Column("issuer_sha256", sa.String(64), nullable=False),
        sa.Column("session_reference_sha256", sa.String(64), nullable=False),
        sa.Column("external_subject", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("token_issued_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("token_expires_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("revoked_at", mysql.DATETIME(fsp=6)),
        sa.Column("revocation_reason", sa.String(64)),
        sa.CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_mm_auth_session_status",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND revoked_at IS NULL AND revocation_reason IS NULL) OR "
            "(status = 'revoked' AND revoked_at IS NOT NULL "
            "AND revocation_reason IS NOT NULL)",
            name="ck_mm_auth_session_revocation_state",
        ),
        sa.UniqueConstraint(
            "issuer_sha256",
            "session_reference_sha256",
            name="uq_mm_auth_session_issuer_reference",
        ),
        **_OPTIONS,
    )
    op.create_index(
        "ix_mm_auth_session_subject_status",
        "mm_auth_sessions",
        ["external_subject", "status"],
    )


def downgrade() -> None:
    op.drop_table("mm_auth_sessions")
