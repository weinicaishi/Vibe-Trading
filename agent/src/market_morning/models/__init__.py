"""SQLAlchemy models for the first Market Morning foundation migration."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    ForeignKey,
    Float,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME, JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now_naive() -> datetime:
    """Return UTC wall time for MySQL ``DATETIME(6)`` columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    """Declarative metadata root for the isolated product schema."""


class User(Base):
    __tablename__ = "mm_users"
    __table_args__ = (
        UniqueConstraint("external_subject", name="uq_mm_users_external_subject"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    external_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Tokyo")
    email_opt_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    account_status: Mapped[str] = mapped_column(String(32), nullable=False, default="invited")
    trial_or_subscription_status: Mapped[str] = mapped_column(String(32), nullable=False, default="private_beta")
    last_product_activity_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=utc_now_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), nullable=False, default=utc_now_naive, onupdate=utc_now_naive
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))


class PrivateBetaInvite(Base):
    """One-time private-beta invitation; the bearer secret is never persisted."""

    __tablename__ = "mm_private_beta_invites"
    __table_args__ = (
        UniqueConstraint(
            "token_sha256",
            name="uq_mm_private_beta_invite_token_sha256",
        ),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'revoked', 'expired')",
            name="ck_mm_private_beta_invite_status",
        ),
        Index("ix_mm_private_beta_invite_status_expires", "status", "expires_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    invite_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    token_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    created_by_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    accepted_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("mm_users.user_id", ondelete="SET NULL")
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    revoked_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class UserConsent(Base):
    __tablename__ = "mm_user_consents"
    __table_args__ = (
        UniqueConstraint("user_id", "consent_type", "consent_version", name="uq_mm_user_consent_version"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    consent_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("mm_users.user_id", ondelete="CASCADE"), nullable=False)
    consent_type: Mapped[str] = mapped_column(String(64), nullable=False)
    consent_version: Mapped[str] = mapped_column(String(64), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=utc_now_naive)
    revoked_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))


class IssuerSnapshot(Base):
    __tablename__ = "mm_issuer_snapshots"
    __table_args__ = (
        UniqueConstraint("source_provider", "source_version", name="uq_mm_issuer_snapshot_source_version"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    source_version: Mapped[str] = mapped_column(String(128), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    import_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    fetched_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=utc_now_naive)
    imported_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))


class Issuer(Base):
    __tablename__ = "mm_issuers"
    __table_args__ = (
        UniqueConstraint("issuer_code", "effective_from", name="uq_mm_issuer_code_effective_from"),
        Index("ix_mm_issuers_search_key", "normalized_search_key"),
        Index("ix_mm_issuers_active_code", "active_status", "issuer_code"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    issuer_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    issuer_code: Mapped[str] = mapped_column(String(12), nullable=False)
    legal_name_ja: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_search_key: Mapped[str] = mapped_column(String(255, collation="utf8mb4_bin"), nullable=False)
    market_segment: Mapped[str] = mapped_column(String(64), nullable=False)
    source_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("mm_issuer_snapshots.snapshot_id", ondelete="RESTRICT"), nullable=False
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    active_status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=utc_now_naive)


class IssuerAlias(Base):
    __tablename__ = "mm_issuer_aliases"
    __table_args__ = (
        UniqueConstraint("issuer_id", "normalized_alias", name="uq_mm_issuer_alias"),
        UniqueConstraint("approved_search_key", name="uq_mm_issuer_alias_approved_search_key"),
        Index("ix_mm_issuer_alias_search", "review_status", "normalized_alias"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    alias_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    issuer_id: Mapped[str] = mapped_column(ForeignKey("mm_issuers.issuer_id", ondelete="CASCADE"), nullable=False)
    display_alias: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(255, collation="utf8mb4_bin"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(Text)
    review_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    approved_search_key: Mapped[str | None] = mapped_column(
        String(255, collation="utf8mb4_bin"),
        Computed(
            "CASE WHEN review_status = 'approved' AND effective_to IS NULL THEN normalized_alias ELSE NULL END",
            persisted=True,
        ),
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=utc_now_naive)


class AuditLog(Base):
    __tablename__ = "mm_audit_logs"
    __table_args__ = (
        Index("ix_mm_audit_entity", "entity_type", "entity_id", "occurred_at"),
        Index("ix_mm_audit_actor", "actor_user_id", "occurred_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    audit_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("mm_users.user_id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    occurred_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=utc_now_naive)


class WatchlistItem(Base):
    __tablename__ = "mm_watchlist_items"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "active_issuer_id",
            name="uq_mm_watchlist_active_user_issuer",
        ),
        Index(
            "ix_mm_watchlist_user_active_order",
            "user_id",
            "removed_at",
            "sort_order",
            "created_at",
        ),
        Index("ix_mm_watchlist_issuer_active", "issuer_id", "removed_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    watchlist_item_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("mm_users.user_id", ondelete="CASCADE"), nullable=False)
    issuer_id: Mapped[str] = mapped_column(ForeignKey("mm_issuers.issuer_id", ondelete="RESTRICT"), nullable=False)
    user_label: Mapped[str | None] = mapped_column(String(40))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=utc_now_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), nullable=False, default=utc_now_naive, onupdate=utc_now_naive
    )
    removed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    active_issuer_id: Mapped[str | None] = mapped_column(
        String(36),
        Computed(
            "CASE WHEN removed_at IS NULL THEN issuer_id ELSE NULL END",
            persisted=True,
        ),
    )


class AccountDeletionRequest(Base):
    __tablename__ = "mm_account_deletion_requests"
    __table_args__ = (
        UniqueConstraint(
            "active_user_id",
            name="uq_mm_account_deletion_active_user",
        ),
        Index("ix_mm_account_deletion_status_requested", "status", "requested_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("mm_users.user_id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    requested_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=utc_now_naive)
    processing_started_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    completed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), nullable=False, default=utc_now_naive, onupdate=utc_now_naive
    )
    active_user_id: Mapped[str | None] = mapped_column(
        String(36),
        Computed(
            "CASE WHEN status IN ('pending', 'processing') THEN user_id ELSE NULL END",
            persisted=True,
        ),
    )


class AnalyticsEvent(Base):
    __tablename__ = "mm_analytics_events"
    __table_args__ = (
        UniqueConstraint("event_name", "request_id", name="uq_mm_analytics_event_request"),
        Index("ix_mm_analytics_name_occurred", "event_name", "occurred_at"),
        Index("ix_mm_analytics_user_occurred", "user_id", "occurred_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_name: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("mm_users.user_id", ondelete="SET NULL"))
    request_id: Mapped[str | None] = mapped_column(String(64))
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    properties: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=utc_now_naive)


class SourceCursor(Base):
    __tablename__ = "mm_source_cursors"
    __table_args__ = (
        Index("ix_mm_source_cursor_health", "last_error_at", "last_successful_discovery_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    source_provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    cursor_value: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    last_successful_discovery_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    last_error_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=utc_now_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), nullable=False, default=utc_now_naive, onupdate=utc_now_naive
    )


class SourceRecord(Base):
    __tablename__ = "mm_source_records"
    __table_args__ = (
        UniqueConstraint(
            "source_provider",
            "provider_document_id",
            "provider_revision_key",
            name="uq_mm_source_record_provider_document_revision",
        ),
        Index(
            "ix_mm_source_record_document_history",
            "source_provider",
            "provider_document_id",
            "published_at",
        ),
        Index("ix_mm_source_record_status_published", "lifecycle_status", "published_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    source_record_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_document_id: Mapped[str] = mapped_column(String(191), nullable=False)
    provider_revision_key: Mapped[str] = mapped_column(String(128), nullable=False)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    document_type: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    content_hash_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    supersedes_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("mm_source_records.source_record_id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=utc_now_naive)


class NormalizedEvent(Base):
    __tablename__ = "mm_normalized_events"
    __table_args__ = (
        UniqueConstraint("event_family_key", "event_version", name="uq_mm_event_family_version"),
        Index("ix_mm_event_issuer_occurred", "issuer_id", "occurred_at"),
        Index("ix_mm_event_status_occurred", "lifecycle_status", "occurred_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_family_key: Mapped[str] = mapped_column(String(191, collation="utf8mb4_bin"), nullable=False)
    event_version: Mapped[int] = mapped_column(Integer, nullable=False)
    issuer_id: Mapped[str] = mapped_column(ForeignKey("mm_issuers.issuer_id", ondelete="RESTRICT"), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_title: Mapped[str] = mapped_column(String(512, collation="utf8mb4_bin"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    supersedes_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("mm_normalized_events.event_id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=utc_now_naive)


class EventSource(Base):
    __tablename__ = "mm_event_sources"
    __table_args__ = (
        UniqueConstraint("event_id", "source_record_id", name="uq_mm_event_source_link"),
        Index("ix_mm_event_source_record", "source_record_id", "linked_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    event_source_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_id: Mapped[str] = mapped_column(
        ForeignKey("mm_normalized_events.event_id", ondelete="CASCADE"), nullable=False
    )
    source_record_id: Mapped[str] = mapped_column(
        ForeignKey("mm_source_records.source_record_id", ondelete="RESTRICT"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(String(32), nullable=False, default="primary")
    linked_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=utc_now_naive)


class EventMergeCandidate(Base):
    __tablename__ = "mm_event_merge_candidates"
    __table_args__ = (
        UniqueConstraint("left_event_id", "right_event_id", name="uq_mm_event_merge_candidate_pair"),
        Index("ix_mm_event_merge_review", "review_status", "created_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    candidate_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    left_event_id: Mapped[str] = mapped_column(
        ForeignKey("mm_normalized_events.event_id", ondelete="CASCADE"), nullable=False
    )
    right_event_id: Mapped[str] = mapped_column(
        ForeignKey("mm_normalized_events.event_id", ondelete="CASCADE"), nullable=False
    )
    reason: Mapped[str] = mapped_column(String(128), nullable=False)
    title_similarity: Mapped[float] = mapped_column(Float, nullable=False)
    time_distance_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    review_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=utc_now_naive)


class EventBriefRecord(Base):
    """Retryable generation state and immutable terminal EventBrief payload."""

    __tablename__ = "mm_event_briefs"
    __table_args__ = (
        UniqueConstraint(
            "generation_key",
            name="uq_mm_event_brief_generation_key",
        ),
        UniqueConstraint(
            "event_id",
            "event_version",
            "schema_version",
            "model_version",
            "prompt_version",
            name="uq_mm_event_brief_generation_spec",
        ),
        CheckConstraint(
            "status NOT IN ('published', 'degraded') OR "
            "(primary_source_record_id IS NOT NULL AND JSON_LENGTH(source_ids) > 0)",
            name="ck_mm_event_brief_publishable_source",
        ),
        Index(
            "ix_mm_event_brief_event_status",
            "event_id",
            "status",
            "updated_at",
        ),
        Index(
            "ix_mm_event_brief_review",
            "review_status",
            "published_at",
        ),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    brief_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_id: Mapped[str] = mapped_column(
        ForeignKey("mm_normalized_events.event_id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_version: Mapped[int] = mapped_column(Integer, nullable=False)
    generation_key: Mapped[str] = mapped_column(String(191), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    review_status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    spec_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    payload_sha256: Mapped[str | None] = mapped_column(String(64))
    source_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    validation_errors: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    primary_source_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("mm_source_records.source_record_id", ondelete="RESTRICT")
    )
    last_failure_code: Mapped[str | None] = mapped_column(String(64))
    last_failed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    started_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    published_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class EventBriefSourceLinkRecord(Base):
    """Normalized source manifest for one terminal EventBrief."""

    __tablename__ = "mm_event_brief_sources"
    __table_args__ = (
        UniqueConstraint(
            "brief_id",
            "source_record_id",
            name="uq_mm_event_brief_source_link",
        ),
        UniqueConstraint(
            "brief_id",
            "position",
            name="uq_mm_event_brief_source_position",
        ),
        Index("ix_mm_event_brief_source_record", "source_record_id", "created_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    brief_source_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_id
    )
    brief_id: Mapped[str] = mapped_column(
        ForeignKey("mm_event_briefs.brief_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_record_id: Mapped[str] = mapped_column(
        ForeignKey("mm_source_records.source_record_id", ondelete="RESTRICT"),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class ModelUsageEventRecord(Base):
    """One privacy-safe model invocation and its provider-reported usage."""

    __tablename__ = "mm_model_usage_events"
    __table_args__ = (
        UniqueConstraint("usage_key", name="uq_mm_model_usage_key"),
        UniqueConstraint(
            "brief_id",
            "attempt_number",
            name="uq_mm_model_usage_brief_attempt",
        ),
        CheckConstraint(
            "attempt_number >= 1",
            name="ck_mm_model_usage_attempt_positive",
        ),
        CheckConstraint(
            "(input_tokens IS NULL OR input_tokens >= 0) "
            "AND (output_tokens IS NULL OR output_tokens >= 0) "
            "AND (total_tokens IS NULL OR total_tokens >= 0) "
            "AND (billable_cost_micros IS NULL OR billable_cost_micros >= 0)",
            name="ck_mm_model_usage_non_negative",
        ),
        CheckConstraint(
            "(usage_status = 'reported' AND input_tokens IS NOT NULL "
            "AND output_tokens IS NOT NULL AND total_tokens = input_tokens + output_tokens) "
            "OR (usage_status = 'missing' AND input_tokens IS NULL "
            "AND output_tokens IS NULL AND total_tokens IS NULL)",
            name="ck_mm_model_usage_token_state",
        ),
        CheckConstraint(
            "(cost_status = 'reported' AND billable_cost_micros IS NOT NULL "
            "AND currency IS NOT NULL) OR (cost_status = 'unpriced' "
            "AND billable_cost_micros IS NULL AND currency IS NULL)",
            name="ck_mm_model_usage_cost_state",
        ),
        Index(
            "ix_mm_model_usage_occurred_status",
            "occurred_at",
            "usage_status",
            "cost_status",
        ),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    usage_event_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_id
    )
    usage_key: Mapped[str] = mapped_column(String(191), nullable=False)
    brief_id: Mapped[str] = mapped_column(
        ForeignKey("mm_event_briefs.brief_id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    usage_status: Mapped[str] = mapped_column(String(32), nullable=False)
    cost_status: Mapped[str] = mapped_column(String(32), nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger)
    billable_cost_micros: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str | None] = mapped_column(String(3))
    provider_request_id_sha256: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class MorningEditionRecord(Base):
    """Immutable per-user edition snapshot published by a generation run."""

    __tablename__ = "mm_morning_editions"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "edition_date",
            "edition_version",
            name="uq_mm_edition_user_date_version",
        ),
        UniqueConstraint(
            "user_id",
            "generation_key",
            name="uq_mm_edition_user_generation_key",
        ),
        Index(
            "ix_mm_edition_user_date_version",
            "user_id",
            "edition_date",
            "edition_version",
        ),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    edition_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("mm_users.user_id", ondelete="CASCADE"), nullable=False)
    edition_date: Mapped[date] = mapped_column(Date, nullable=False)
    edition_version: Mapped[int] = mapped_column(Integer, nullable=False)
    generation_key: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    supersedes_edition_id: Mapped[str | None] = mapped_column(
        ForeignKey("mm_morning_editions.edition_id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=utc_now_naive)


class ContentReportRecord(Base):
    """One privacy-safe user report for an event in an immutable edition."""

    __tablename__ = "mm_content_reports"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "edition_id",
            "event_id",
            name="uq_mm_content_report_user_edition_event",
        ),
        CheckConstraint(
            "reason_code IN ('fact_inaccurate', 'source_mismatch', "
            "'outdated_or_corrected', 'other_content_issue')",
            name="ck_mm_content_report_reason",
        ),
        CheckConstraint(
            "status IN ('pending', 'resolved', 'dismissed')",
            name="ck_mm_content_report_status",
        ),
        CheckConstraint(
            "(status = 'pending' AND resolution_code IS NULL "
            "AND reviewed_by IS NULL AND reviewed_at IS NULL) OR "
            "(status IN ('resolved', 'dismissed') AND resolution_code IS NOT NULL "
            "AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="ck_mm_content_report_resolution_state",
        ),
        Index("ix_mm_content_report_status_created", "status", "created_at"),
        Index("ix_mm_content_report_event_created", "event_id", "created_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    report_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_id
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("mm_users.user_id", ondelete="CASCADE"), nullable=False
    )
    edition_id: Mapped[str] = mapped_column(
        ForeignKey("mm_morning_editions.edition_id", ondelete="CASCADE"),
        nullable=False,
    )
    event_id: Mapped[str] = mapped_column(
        ForeignKey("mm_normalized_events.event_id", ondelete="RESTRICT"),
        nullable=False,
    )
    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )
    resolution_code: Mapped[str | None] = mapped_column(String(64))
    reviewed_by: Mapped[str | None] = mapped_column(String(128))
    reviewed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class EditionEventStateRecord(Base):
    """Mutable private state for one event inside an immutable user edition."""

    __tablename__ = "mm_edition_event_states"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "edition_id",
            "event_id",
            name="uq_mm_edition_event_state",
        ),
        CheckConstraint(
            "state IN ('read', 'later', 'irrelevant')",
            name="ck_mm_edition_event_state",
        ),
        Index(
            "ix_mm_edition_event_state_user_edition",
            "user_id",
            "edition_id",
            "state",
        ),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    event_state_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_id
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("mm_users.user_id", ondelete="CASCADE"), nullable=False
    )
    edition_id: Mapped[str] = mapped_column(
        ForeignKey("mm_morning_editions.edition_id", ondelete="CASCADE"),
        nullable=False,
    )
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    first_read_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class EditionSourceOpenRecord(Base):
    """Idempotent source-open audit without copying URLs into behavior data."""

    __tablename__ = "mm_edition_source_opens"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "request_id",
            name="uq_mm_edition_source_open_request",
        ),
        Index(
            "ix_mm_edition_source_open_source",
            "source_record_id",
            "opened_at",
        ),
        Index(
            "ix_mm_edition_source_open_user",
            "user_id",
            "opened_at",
        ),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    source_open_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_id
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("mm_users.user_id", ondelete="CASCADE"), nullable=False
    )
    edition_id: Mapped[str] = mapped_column(
        ForeignKey("mm_morning_editions.edition_id", ondelete="CASCADE"),
        nullable=False,
    )
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_record_id: Mapped[str] = mapped_column(
        ForeignKey("mm_source_records.source_record_id", ondelete="RESTRICT"),
        nullable=False,
    )
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class IssuerResearchNoteRecord(Base):
    """One lightweight private research note per user and watched issuer."""

    __tablename__ = "mm_issuer_research_notes"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "issuer_id",
            name="uq_mm_issuer_research_note_user_issuer",
        ),
        Index(
            "ix_mm_issuer_research_note_user_updated",
            "user_id",
            "updated_at",
        ),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    note_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_id
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("mm_users.user_id", ondelete="CASCADE"), nullable=False
    )
    issuer_id: Mapped[str] = mapped_column(
        ForeignKey("mm_issuers.issuer_id", ondelete="RESTRICT"), nullable=False
    )
    note_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class JobRecord(Base):
    """Durable work item claimed by one leased worker at a time."""

    __tablename__ = "mm_jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_mm_job_idempotency_key"),
        Index(
            "ix_mm_job_claim",
            "status",
            "available_at",
            "priority",
            "created_at",
        ),
        Index("ix_mm_job_expired_lease", "status", "lease_expires_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    completed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False, default=utc_now_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), nullable=False, default=utc_now_naive, onupdate=utc_now_naive
    )


class JobAttemptRecord(Base):
    """Append-only attempt history for a durable Market Morning job."""

    __tablename__ = "mm_job_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_mm_job_attempt_number"),
        Index("ix_mm_job_attempt_status", "job_id", "status", "started_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    attempt_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("mm_jobs.job_id", ondelete="CASCADE"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    error_code: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class SchedulerLeaseRecord(Base):
    """Database-backed single-active scheduler lease."""

    __tablename__ = "mm_scheduler_leases"
    __table_args__ = ({"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},)

    lease_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lease_until: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class ManualOverrideRecord(Base):
    """Audited fail-closed operational override for one edition date."""

    __tablename__ = "mm_manual_overrides"
    __table_args__ = (
        UniqueConstraint(
            "active_edition_date",
            name="uq_mm_manual_override_active_date",
        ),
        Index(
            "ix_mm_manual_override_date_status",
            "edition_date",
            "status",
            "created_at",
        ),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    override_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    override_type: Mapped[str] = mapped_column(String(32), nullable=False)
    edition_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    active_edition_date: Mapped[date | None] = mapped_column(
        Date,
        Computed(
            "CASE WHEN override_type = 'publication_halt' AND status = 'active' THEN edition_date ELSE NULL END",
            persisted=True,
        ),
    )
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    revoked_by: Mapped[str | None] = mapped_column(String(128))
    revoked_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class MarketSnapshotRecord(Base):
    """Immutable provider observation used by the publication gate."""

    __tablename__ = "mm_market_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "instrument",
            "provider",
            "session_date",
            "as_of",
            name="uq_mm_market_snapshot_identity",
        ),
        Index(
            "ix_mm_market_snapshot_latest",
            "instrument",
            "session_date",
            "as_of",
        ),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    instrument: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    as_of: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    value: Mapped[Any] = mapped_column(Numeric(24, 8), nullable=False)
    previous_close: Mapped[Any | None] = mapped_column(Numeric(24, 8))
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    delay_status: Mapped[str] = mapped_column(String(16), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class GlobalEditionDayRecord(Base):
    """One durable row per JST edition date used as the aggregate lock."""

    __tablename__ = "mm_global_edition_days"
    __table_args__ = ({"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},)

    edition_date: Mapped[date] = mapped_column(Date, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class GlobalEditionRunRecord(Base):
    """Versioned global publication attempt for one JST edition date."""

    __tablename__ = "mm_global_edition_runs"
    __table_args__ = (
        UniqueConstraint(
            "edition_date",
            "run_version",
            name="uq_mm_global_run_date_version",
        ),
        UniqueConstraint(
            "generation_key",
            name="uq_mm_global_run_generation_key",
        ),
        UniqueConstraint(
            "current_success_date",
            name="uq_mm_global_run_current_success",
        ),
        Index("ix_mm_global_run_date_status", "edition_date", "status", "started_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    edition_date: Mapped[date] = mapped_column(
        ForeignKey("mm_global_edition_days.edition_date", ondelete="RESTRICT"),
        nullable=False,
    )
    run_version: Mapped[int] = mapped_column(Integer, nullable=False)
    generation_key: Mapped[str] = mapped_column(String(191), nullable=False)
    attempt_key: Mapped[str] = mapped_column(String(16), nullable=False)
    scenario: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    current_success_date: Mapped[date | None] = mapped_column(
        Date,
        Computed(
            "CASE WHEN is_current = 1 AND status IN ('complete', 'partial', 'late') THEN edition_date ELSE NULL END",
            persisted=True,
        ),
    )
    email_permitted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    late: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64))
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    spec_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class GlobalEditionItemRecord(Base):
    """Ordered immutable item manifest for a successful global run."""

    __tablename__ = "mm_global_edition_items"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "position",
            name="uq_mm_global_item_run_position",
        ),
        UniqueConstraint(
            "run_id",
            "snapshot_id",
            name="uq_mm_global_item_run_snapshot",
        ),
        Index("ix_mm_global_item_run_type", "run_id", "item_type", "position"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    item_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("mm_global_edition_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    item_type: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("mm_market_snapshots.snapshot_id", ondelete="RESTRICT"),
        nullable=False,
    )
    instrument: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class DeliveryAttemptRecord(Base):
    """Idempotent email reminder lifecycle without raw address or token storage."""

    __tablename__ = "mm_delivery_attempts"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "edition_date",
            "channel",
            name="uq_mm_delivery_user_date_channel",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_mm_delivery_idempotency_key",
        ),
        UniqueConstraint(
            "provider_message_id",
            name="uq_mm_delivery_provider_message",
        ),
        CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'delivered', "
            "'failed', 'clicked', 'suppressed')",
            name="ck_mm_delivery_status",
        ),
        Index(
            "ix_mm_delivery_status_requested",
            "status",
            "requested_at",
        ),
        Index(
            "ix_mm_delivery_user_date",
            "user_id",
            "edition_date",
            "requested_at",
        ),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    delivery_attempt_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_id
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("mm_users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    global_run_id: Mapped[str] = mapped_column(
        ForeignKey("mm_global_edition_runs.run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    edition_date: Mapped[date] = mapped_column(Date, nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="email")
    idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    provider_message_id: Mapped[str | None] = mapped_column(String(191))
    deep_link_token_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    requested_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    delivered_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    clicked_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    failed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)


class DeliveryProviderEventRecord(Base):
    """Hash-only, idempotent audit envelope for verified provider webhooks."""

    __tablename__ = "mm_delivery_provider_events"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_event_id",
            name="uq_mm_delivery_provider_event",
        ),
        CheckConstraint(
            "event_type IN ('delivered', 'failed', 'clicked')",
            name="ck_mm_delivery_provider_event_type",
        ),
        CheckConstraint(
            "outcome IN ('pending', 'applied', 'stale', 'unmatched', 'rejected')",
            name="ck_mm_delivery_provider_event_outcome",
        ),
        Index(
            "ix_mm_delivery_provider_message_received",
            "provider_message_id",
            "received_at",
        ),
        Index(
            "ix_mm_delivery_provider_outcome",
            "outcome",
            "received_at",
        ),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    provider_event_row_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=new_id
    )
    delivery_attempt_id: Mapped[str | None] = mapped_column(
        ForeignKey("mm_delivery_attempts.delivery_attempt_id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(191), nullable=False)
    provider_message_id: Mapped[str] = mapped_column(String(191), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    error_code: Mapped[str | None] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))


__all__ = [
    "AccountDeletionRequest",
    "AnalyticsEvent",
    "AuditLog",
    "Base",
    "ContentReportRecord",
    "DeliveryAttemptRecord",
    "DeliveryProviderEventRecord",
    "EditionEventStateRecord",
    "EditionSourceOpenRecord",
    "EventMergeCandidate",
    "EventBriefRecord",
    "EventBriefSourceLinkRecord",
    "EventSource",
    "GlobalEditionDayRecord",
    "GlobalEditionItemRecord",
    "GlobalEditionRunRecord",
    "Issuer",
    "IssuerAlias",
    "IssuerResearchNoteRecord",
    "IssuerSnapshot",
    "JobAttemptRecord",
    "JobRecord",
    "ManualOverrideRecord",
    "MarketSnapshotRecord",
    "ModelUsageEventRecord",
    "MorningEditionRecord",
    "NormalizedEvent",
    "PrivateBetaInvite",
    "SourceCursor",
    "SourceRecord",
    "SchedulerLeaseRecord",
    "User",
    "UserConsent",
    "WatchlistItem",
]
