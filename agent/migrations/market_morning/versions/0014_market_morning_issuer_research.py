"""Add lightweight private issuer research notes.

Revision ID: 0014_market_morning_issuer_research
Revises: 0013_market_morning_edition_interactions
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = "0014_market_morning_issuer_research"
down_revision: str | None = "0013_market_morning_edition_interactions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def upgrade() -> None:
    op.create_table(
        "mm_issuer_research_notes",
        sa.Column("note_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("issuer_id", sa.String(36), nullable=False),
        sa.Column("note_text", sa.Text(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["mm_users.user_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["issuer_id"], ["mm_issuers.issuer_id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "user_id",
            "issuer_id",
            name="uq_mm_issuer_research_note_user_issuer",
        ),
        **_OPTIONS,
    )
    op.create_index(
        "ix_mm_issuer_research_note_user_updated",
        "mm_issuer_research_notes",
        ["user_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_table("mm_issuer_research_notes")
