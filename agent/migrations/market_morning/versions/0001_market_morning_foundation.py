"""Create Market Morning account and issuer-master foundation.

Revision ID: 0001_market_morning_foundation
Revises:
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = "0001_market_morning_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = ("market_morning",)
depends_on: str | Sequence[str] | None = None

_TABLE_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def upgrade() -> None:
    # Alembic creates its version table with VARCHAR(32) before invoking the
    # first revision. Several Market Morning revision IDs already exceed that
    # limit (the first is 0004_market_morning_sources_events), so widen it at
    # the start of the migration chain, before Alembic records any long ID.
    # Keep the wider column permanently; downgrading to base only clears the
    # version row and must not make a later re-upgrade fail again.
    op.alter_column(
        "alembic_version",
        "version_num",
        existing_type=sa.String(32),
        type_=sa.String(64),
        existing_nullable=False,
    )
    op.create_table(
        "mm_users",
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column("external_subject", sa.String(255), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Tokyo"),
        sa.Column("email_opt_in", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("account_status", sa.String(32), nullable=False, server_default="invited"),
        sa.Column(
            "trial_or_subscription_status",
            sa.String(32),
            nullable=False,
            server_default="private_beta",
        ),
        sa.Column("last_product_activity_at", mysql.DATETIME(fsp=6)),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("deleted_at", mysql.DATETIME(fsp=6)),
        sa.UniqueConstraint("external_subject", name="uq_mm_users_external_subject"),
        **_TABLE_OPTIONS,
    )

    op.create_table(
        "mm_user_consents",
        sa.Column("consent_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("consent_type", sa.String(64), nullable=False),
        sa.Column("consent_version", sa.String(64), nullable=False),
        sa.Column("accepted_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("revoked_at", mysql.DATETIME(fsp=6)),
        sa.ForeignKeyConstraint(["user_id"], ["mm_users.user_id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "user_id", "consent_type", "consent_version", name="uq_mm_user_consent_version"
        ),
        **_TABLE_OPTIONS,
    )

    op.create_table(
        "mm_issuer_snapshots",
        sa.Column("snapshot_id", sa.String(36), primary_key=True),
        sa.Column("source_provider", sa.String(64), nullable=False),
        sa.Column("source_version", sa.String(128), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("checksum_sha256", sa.String(64), nullable=False),
        sa.Column("import_status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("fetched_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("imported_at", mysql.DATETIME(fsp=6)),
        sa.UniqueConstraint(
            "source_provider", "source_version", name="uq_mm_issuer_snapshot_source_version"
        ),
        **_TABLE_OPTIONS,
    )

    op.create_table(
        "mm_issuers",
        sa.Column("issuer_id", sa.String(36), primary_key=True),
        sa.Column("issuer_code", sa.String(12), nullable=False),
        sa.Column("legal_name_ja", sa.String(255), nullable=False),
        sa.Column(
            "normalized_search_key",
            sa.String(255, collation="utf8mb4_bin"),
            nullable=False,
        ),
        sa.Column("market_segment", sa.String(64), nullable=False),
        sa.Column("source_snapshot_id", sa.String(36), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date()),
        sa.Column("active_status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_snapshot_id"], ["mm_issuer_snapshots.snapshot_id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "issuer_code", "effective_from", name="uq_mm_issuer_code_effective_from"
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index("ix_mm_issuers_search_key", "mm_issuers", ["normalized_search_key"])
    op.create_index("ix_mm_issuers_active_code", "mm_issuers", ["active_status", "issuer_code"])

    op.create_table(
        "mm_issuer_aliases",
        sa.Column("alias_id", sa.String(36), primary_key=True),
        sa.Column("issuer_id", sa.String(36), nullable=False),
        sa.Column("display_alias", sa.String(255), nullable=False),
        sa.Column(
            "normalized_alias",
            sa.String(255, collation="utf8mb4_bin"),
            nullable=False,
        ),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_reference", sa.Text()),
        sa.Column("review_status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date()),
        sa.Column(
            "approved_search_key",
            sa.String(255, collation="utf8mb4_bin"),
            sa.Computed(
                "CASE WHEN review_status = 'approved' AND effective_to IS NULL "
                "THEN normalized_alias ELSE NULL END",
                persisted=True,
            ),
        ),
        sa.Column("reviewed_by", sa.String(255)),
        sa.Column("reviewed_at", mysql.DATETIME(fsp=6)),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(["issuer_id"], ["mm_issuers.issuer_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("issuer_id", "normalized_alias", name="uq_mm_issuer_alias"),
        sa.UniqueConstraint(
            "approved_search_key", name="uq_mm_issuer_alias_approved_search_key"
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_mm_issuer_alias_search",
        "mm_issuer_aliases",
        ["review_status", "normalized_alias"],
    )

    op.create_table(
        "mm_audit_logs",
        sa.Column("audit_id", sa.String(36), primary_key=True),
        sa.Column("actor_user_id", sa.String(36)),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=False),
        sa.Column("details", mysql.JSON()),
        sa.Column("occurred_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["mm_users.user_id"], ondelete="SET NULL"),
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_mm_audit_entity",
        "mm_audit_logs",
        ["entity_type", "entity_id", "occurred_at"],
    )
    op.create_index(
        "ix_mm_audit_actor",
        "mm_audit_logs",
        ["actor_user_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_table("mm_audit_logs")
    op.drop_table("mm_issuer_aliases")
    op.drop_table("mm_issuers")
    op.drop_table("mm_issuer_snapshots")
    op.drop_table("mm_user_consents")
    op.drop_table("mm_users")
