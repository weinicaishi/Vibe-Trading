"""Transactional orchestration from persisted official events to an edition."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.db import get_session_factory
from src.market_morning.models import (
    EventSource,
    EventBriefRecord,
    Issuer,
    NormalizedEvent,
    SourceRecord,
    User,
    WatchlistItem,
)
from src.market_morning.pipeline.morning_edition import (
    EditionDayPlan,
    EditionEventInput,
    EditionEventFactInput,
    EditionIssuerInput,
    EditionSourceCitation,
    GenerationBudget,
    IssuerCollectionStatus,
    generate_morning_edition,
)
from src.market_morning.repositories.morning_edition import (
    EditionUserUnavailable,
    PublishedMorningEdition,
    publish_morning_edition,
)
from src.market_morning.repositories.event_briefs import (
    event_brief_payload_sha256,
)
from src.market_morning.source_coverage import (
    CoverageIssuer,
    IssuerSourceCoverage,
    SourceCoveragePolicy,
    load_issuer_source_coverages,
)

DEFAULT_GENERATION_WINDOW = timedelta(hours=24)
MAX_GENERATION_WINDOW = timedelta(hours=72)
MAX_GENERATION_SOURCE_ROWS = 300


class EditionGenerationError(RuntimeError):
    """Base class for expected edition-generation failures."""


class EditionGenerationSourceRowsExceeded(EditionGenerationError):
    """Raised instead of silently publishing a truncated event corpus."""


def _aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _mysql_utc(value: datetime, *, field_name: str) -> datetime:
    return _aware(value, field_name=field_name).astimezone(timezone.utc).replace(tzinfo=None)


def _utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _uuid(value: str, *, field_name: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise ValueError(f"{field_name} must be a UUID") from error


def _bounded(value: int, *, field_name: str, maximum: int) -> int:
    if not 1 <= value <= maximum:
        raise ValueError(f"{field_name} must be between 1 and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class EditionGenerationWindow:
    """Inclusive UTC event-time bounds used for one generation run."""

    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        starts_at = _aware(self.starts_at, field_name="starts_at")
        ends_at = _aware(self.ends_at, field_name="ends_at")
        duration = ends_at - starts_at
        if duration <= timedelta(0):
            raise ValueError("ends_at must be later than starts_at")
        if duration > MAX_GENERATION_WINDOW:
            raise ValueError("generation window cannot exceed 72 hours")

    @classmethod
    def ending_at(
        cls,
        ends_at: datetime,
        *,
        duration: timedelta = DEFAULT_GENERATION_WINDOW,
    ) -> "EditionGenerationWindow":
        _aware(ends_at, field_name="ends_at")
        return cls(starts_at=ends_at - duration, ends_at=ends_at)

    @property
    def mysql_bounds(self) -> tuple[datetime, datetime]:
        return (
            _mysql_utc(self.starts_at, field_name="starts_at"),
            _mysql_utc(self.ends_at, field_name="ends_at"),
        )


@dataclass(frozen=True, slots=True)
class EditionGenerationCommand:
    user_id: str
    generation_key: str
    day_plan: EditionDayPlan
    generated_at: datetime
    published_at: datetime
    window: EditionGenerationWindow | None = None
    budget: GenerationBudget | None = None
    collection_status_by_issuer: Mapping[str, IssuerCollectionStatus | str] | None = None
    source_coverage_policy: SourceCoveragePolicy | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", _uuid(self.user_id, field_name="user_id"))
        generation_key = self.generation_key.strip()
        if not generation_key or len(generation_key) > 128:
            raise ValueError("generation_key must contain 1 to 128 characters")
        object.__setattr__(self, "generation_key", generation_key)
        generated_at = _aware(self.generated_at, field_name="generated_at")
        published_at = _aware(self.published_at, field_name="published_at")
        if published_at < generated_at:
            raise ValueError("published_at cannot precede generated_at")
        if self.collection_status_by_issuer is not None and self.source_coverage_policy is not None:
            raise ValueError("collection_status_by_issuer and source_coverage_policy are mutually exclusive")
        window = self.window or EditionGenerationWindow.ending_at(generated_at)
        if window.ends_at > generated_at:
            raise ValueError("generation window cannot end after generated_at")
        object.__setattr__(self, "window", window)


def build_generation_watchlist_statement(user_id: str, *, limit: int = 10):
    canonical_user_id = _uuid(user_id, field_name="user_id")
    bounded_limit = _bounded(limit, field_name="limit", maximum=10)
    return (
        select(
            Issuer.issuer_id,
            Issuer.issuer_code,
            Issuer.legal_name_ja,
            WatchlistItem.sort_order,
        )
        .select_from(WatchlistItem)
        .join(User, User.user_id == WatchlistItem.user_id)
        .join(Issuer, Issuer.issuer_id == WatchlistItem.issuer_id)
        .where(
            WatchlistItem.user_id == canonical_user_id,
            User.account_status == "active",
            User.deleted_at.is_(None),
            WatchlistItem.removed_at.is_(None),
            Issuer.active_status == "active",
            Issuer.effective_to.is_(None),
        )
        .order_by(
            WatchlistItem.sort_order,
            WatchlistItem.created_at,
            WatchlistItem.watchlist_item_id,
        )
        .limit(bounded_limit)
    )


def _latest_event_brief_subquery():
    ranked = (
        select(
            EventBriefRecord.event_id,
            EventBriefRecord.event_version,
            EventBriefRecord.status.label("brief_status"),
            EventBriefRecord.review_status.label("brief_review_status"),
            EventBriefRecord.payload.label("brief_payload"),
            EventBriefRecord.payload_sha256.label("brief_payload_sha256"),
            func.row_number()
            .over(
                partition_by=(
                    EventBriefRecord.event_id,
                    EventBriefRecord.event_version,
                ),
                order_by=(
                    case(
                        (EventBriefRecord.review_status == "approved", 0),
                        (EventBriefRecord.review_status == "auto_validated", 1),
                        else_=2,
                    ),
                    case(
                        (EventBriefRecord.status == "published", 0),
                        (EventBriefRecord.status == "degraded", 1),
                        else_=2,
                    ),
                    EventBriefRecord.completed_at.desc(),
                    EventBriefRecord.brief_id.desc(),
                ),
            )
            .label("brief_rank"),
        )
        .where(
            EventBriefRecord.status.in_(("published", "degraded", "blocked")),
            EventBriefRecord.review_status != "rejected",
        )
        .subquery("ranked_event_briefs")
    )
    return (
        select(
            ranked.c.event_id,
            ranked.c.event_version,
            ranked.c.brief_status,
            ranked.c.brief_review_status,
            ranked.c.brief_payload,
            ranked.c.brief_payload_sha256,
        )
        .where(ranked.c.brief_rank == 1)
        .subquery("latest_event_briefs")
    )


def _generation_event_columns(latest_brief):
    return (
        NormalizedEvent.event_id,
        NormalizedEvent.event_family_key,
        NormalizedEvent.event_version,
        NormalizedEvent.issuer_id,
        NormalizedEvent.title,
        NormalizedEvent.event_type,
        NormalizedEvent.occurred_at,
        NormalizedEvent.lifecycle_status.label("event_lifecycle_status"),
        SourceRecord.source_record_id,
        SourceRecord.source_provider,
        SourceRecord.provider_document_id,
        SourceRecord.provider_revision_key,
        SourceRecord.original_url,
        SourceRecord.published_at.label("source_published_at"),
        latest_brief.c.brief_status,
        latest_brief.c.brief_review_status,
        latest_brief.c.brief_payload,
        latest_brief.c.brief_payload_sha256,
    )


def build_generation_events_statement(
    issuer_ids: tuple[str, ...],
    *,
    window: EditionGenerationWindow,
    max_source_rows: int = MAX_GENERATION_SOURCE_ROWS,
):
    if not issuer_ids:
        raise ValueError("issuer_ids must not be empty")
    canonical_issuer_ids = tuple(_uuid(issuer_id, field_name="issuer_id") for issuer_id in issuer_ids)
    bounded_rows = _bounded(
        max_source_rows,
        field_name="max_source_rows",
        maximum=1_000,
    )
    starts_at, ends_at = window.mysql_bounds
    latest_brief = _latest_event_brief_subquery()
    return (
        select(*_generation_event_columns(latest_brief))
        .select_from(NormalizedEvent)
        .join(EventSource, EventSource.event_id == NormalizedEvent.event_id)
        .join(
            SourceRecord,
            SourceRecord.source_record_id == EventSource.source_record_id,
        )
        .outerjoin(
            latest_brief,
            (latest_brief.c.event_id == NormalizedEvent.event_id)
            & (latest_brief.c.event_version == NormalizedEvent.event_version),
        )
        .where(
            NormalizedEvent.issuer_id.in_(canonical_issuer_ids),
            NormalizedEvent.occurred_at >= starts_at,
            NormalizedEvent.occurred_at <= ends_at,
        )
        .order_by(
            NormalizedEvent.issuer_id,
            NormalizedEvent.occurred_at.desc(),
            NormalizedEvent.event_version.desc(),
            NormalizedEvent.event_id,
            SourceRecord.source_provider,
            SourceRecord.provider_document_id,
            SourceRecord.provider_revision_key,
        )
        .limit(bounded_rows + 1)
    )


def _coverage(
    issuer_id: str,
    explicit: Mapping[str, IssuerSourceCoverage | IssuerCollectionStatus | str] | None,
) -> tuple[IssuerCollectionStatus, str | None]:
    raw_coverage = None if explicit is None else explicit.get(issuer_id)
    if isinstance(raw_coverage, IssuerSourceCoverage):
        return raw_coverage.status, raw_coverage.error_code
    raw_status = raw_coverage
    status = IssuerCollectionStatus.PARTIAL if raw_status is None else IssuerCollectionStatus(raw_status)
    if status is IssuerCollectionStatus.COMPLETE:
        return status, None
    if status is IssuerCollectionStatus.UNAVAILABLE:
        return status, "source_unavailable"
    return status, "source_coverage_unverified"


def _event_inputs(rows: tuple[Mapping[str, Any], ...]) -> dict[str, list[EditionEventInput]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        event_id = row["event_id"]
        group = grouped.setdefault(
            event_id,
            {
                "event_id": event_id,
                "event_family_key": row["event_family_key"],
                "event_version": row["event_version"],
                "issuer_id": row["issuer_id"],
                "title": row["title"],
                "event_type": row["event_type"],
                "occurred_at": _utc_aware(row["occurred_at"]),
                "lifecycle_status": row["event_lifecycle_status"],
                "citations": {},
                "brief_status": row.get("brief_status"),
                "brief_review_status": row.get("brief_review_status"),
                "brief_payload": row.get("brief_payload"),
                "brief_payload_sha256": row.get("brief_payload_sha256"),
            },
        )
        try:
            citation = EditionSourceCitation(
                provider=row["source_provider"],
                document_id=row["provider_document_id"],
                revision_key=row["provider_revision_key"],
                original_url=row["original_url"],
                published_at=_utc_aware(row["source_published_at"]),
            )
        except (TypeError, ValueError):
            # The deterministic core will omit an event with no valid citation
            # and mark the brief partial instead of crashing the whole edition.
            continue
        group["citations"][row["source_record_id"]] = citation

    by_issuer: dict[str, list[EditionEventInput]] = {}
    for group in grouped.values():
        citations = tuple(
            sorted(
                group["citations"].values(),
                key=lambda item: (
                    item.provider,
                    item.document_id,
                    item.revision_key,
                    item.original_url,
                ),
            )
        )
        brief_facts, brief_warnings = _event_brief_facts(group)
        event = EditionEventInput(
            event_id=group["event_id"],
            event_family_key=group["event_family_key"],
            event_version=group["event_version"],
            title=group["title"],
            event_type=group["event_type"],
            occurred_at=group["occurred_at"],
            lifecycle_status=group["lifecycle_status"],
            citations=citations,
            facts=brief_facts,
            warnings=brief_warnings,
        )
        by_issuer.setdefault(group["issuer_id"], []).append(event)

    for events in by_issuer.values():
        events.sort(
            key=lambda item: (
                -item.occurred_at.timestamp(),
                item.event_family_key,
                -item.event_version,
                item.event_id,
            )
        )
    return by_issuer


_EVENT_BRIEF_PAYLOAD_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "event_id",
        "event_version",
        "title",
        "occurred_at",
        "confirmed_facts",
        "open_questions",
        "source_ids",
        "sources",
        "model_version",
        "review_status",
        "error_codes",
    }
)


def _event_brief_facts(
    group: Mapping[str, Any],
) -> tuple[tuple[EditionEventFactInput, ...], tuple[str, ...]]:
    status = group.get("brief_status")
    if status is None:
        return (), ("event_brief_unavailable",)
    if status == "degraded":
        return (), ("event_brief_degraded",)
    if status == "blocked":
        return (), ("event_brief_blocked",)
    if status != "published":
        return (), ("event_brief_invalid",)

    payload = group.get("brief_payload")
    digest = group.get("brief_payload_sha256")
    try:
        if (
            not isinstance(payload, dict)
            or frozenset(payload) != _EVENT_BRIEF_PAYLOAD_FIELDS
            or not isinstance(digest, str)
            or event_brief_payload_sha256(payload) != digest
            or payload["schema_version"] != 1
            or payload["status"] != "published"
            or payload["event_id"] != group["event_id"]
            or payload["event_version"] != group["event_version"]
            or payload["review_status"] not in {"auto_validated", "approved"}
            or group.get("brief_review_status")
            not in {"auto_validated", "approved"}
        ):
            raise ValueError("event brief envelope is invalid")
        declared_ids = payload["source_ids"]
        raw_facts = payload["confirmed_facts"]
        if (
            not isinstance(declared_ids, list)
            or not declared_ids
            or any(not isinstance(value, str) for value in declared_ids)
            or len(set(declared_ids)) != len(declared_ids)
            or not isinstance(raw_facts, list)
            or not raw_facts
        ):
            raise ValueError("event brief facts are invalid")
        citations_by_id = group["citations"]
        if any(source_id not in citations_by_id for source_id in declared_ids):
            raise ValueError("event brief source is unavailable")

        facts: list[EditionEventFactInput] = []
        for raw_fact in raw_facts:
            if not isinstance(raw_fact, dict) or frozenset(raw_fact) != {
                "text",
                "source_ids",
            }:
                raise ValueError("event brief fact is invalid")
            source_ids = raw_fact["source_ids"]
            if (
                not isinstance(raw_fact["text"], str)
                or not isinstance(source_ids, list)
                or not source_ids
                or any(not isinstance(value, str) for value in source_ids)
                or any(source_id not in declared_ids for source_id in source_ids)
            ):
                raise ValueError("event brief fact citation is invalid")
            facts.append(
                EditionEventFactInput(
                    text=raw_fact["text"],
                    citations=tuple(citations_by_id[source_id] for source_id in source_ids),
                )
            )
        return tuple(facts), ()
    except (KeyError, TypeError, ValueError):
        return (), ("event_brief_invalid",)


async def load_generation_inputs(
    session: AsyncSession,
    *,
    user_id: str,
    window: EditionGenerationWindow,
    max_issuers: int = 10,
    max_source_rows: int = MAX_GENERATION_SOURCE_ROWS,
    collection_status_by_issuer: Mapping[str, IssuerCollectionStatus | str] | None = None,
    source_coverage_policy: SourceCoveragePolicy | None = None,
) -> tuple[EditionIssuerInput, ...]:
    """Load one consistent user/watchlist/event snapshot under a user lock."""
    canonical_user_id = _uuid(user_id, field_name="user_id")
    active_user = (
        await session.execute(select(User).where(User.user_id == canonical_user_id).with_for_update())
    ).scalar_one_or_none()
    if active_user is None or active_user.deleted_at is not None or active_user.account_status != "active":
        raise EditionUserUnavailable("active Market Morning user is required")

    issuer_rows = (
        (
            await session.execute(
                build_generation_watchlist_statement(
                    canonical_user_id,
                    limit=max_issuers,
                )
            )
        )
        .mappings()
        .all()
    )
    issuer_rows = tuple(issuer_rows)
    if not issuer_rows:
        return ()

    resolved_coverage: Mapping[str, IssuerSourceCoverage | IssuerCollectionStatus | str] | None = (
        collection_status_by_issuer
    )
    if source_coverage_policy is not None:
        if collection_status_by_issuer is not None:
            raise ValueError("collection_status_by_issuer and source_coverage_policy are mutually exclusive")
        resolved_coverage = await load_issuer_source_coverages(
            session,
            issuers=tuple(
                CoverageIssuer(
                    issuer_id=row["issuer_id"],
                    issuer_code=row["issuer_code"],
                )
                for row in issuer_rows
            ),
            policy=source_coverage_policy,
            as_of=window.ends_at,
        )

    issuer_ids = tuple(row["issuer_id"] for row in issuer_rows)
    source_rows = (
        (
            await session.execute(
                build_generation_events_statement(
                    issuer_ids,
                    window=window,
                    max_source_rows=max_source_rows,
                )
            )
        )
        .mappings()
        .all()
    )
    if len(source_rows) > max_source_rows:
        raise EditionGenerationSourceRowsExceeded("generation source row budget exceeded; edition was not published")
    events_by_issuer = _event_inputs(tuple(source_rows))

    inputs: list[EditionIssuerInput] = []
    for row in issuer_rows:
        issuer_id = row["issuer_id"]
        collection_status, error_code = _coverage(
            issuer_id,
            resolved_coverage,
        )
        inputs.append(
            EditionIssuerInput(
                issuer_id=issuer_id,
                issuer_code=row["issuer_code"],
                legal_name_ja=row["legal_name_ja"],
                collection_status=collection_status,
                events=tuple(events_by_issuer.get(issuer_id, ())),
                error_code=error_code,
            )
        )
    return tuple(inputs)


EditionPublisher = Callable[..., Awaitable[PublishedMorningEdition]]


async def generate_and_publish_morning_edition(
    session: AsyncSession,
    *,
    command: EditionGenerationCommand,
    publisher: EditionPublisher = publish_morning_edition,
) -> PublishedMorningEdition:
    """Generate and publish inside the caller's existing transaction."""
    issuers: tuple[EditionIssuerInput, ...] = ()
    if command.day_plan.generate:
        assert command.window is not None
        issuers = await load_generation_inputs(
            session,
            user_id=command.user_id,
            window=command.window,
            max_issuers=(command.budget or GenerationBudget()).max_issuers,
            collection_status_by_issuer=command.collection_status_by_issuer,
            source_coverage_policy=command.source_coverage_policy,
        )
    edition = generate_morning_edition(
        day_plan=command.day_plan,
        issuers=issuers,
        generated_at=command.generated_at,
        budget=command.budget,
    )
    return await publisher(
        session,
        user_id=command.user_id,
        generation_key=command.generation_key,
        edition=edition,
        published_at=command.published_at,
    )


async def run_morning_edition_generation(
    command: EditionGenerationCommand,
) -> PublishedMorningEdition:
    """Production entry point: load, generate, and publish in one transaction."""
    factory = get_session_factory()
    async with factory.begin() as session:
        return await generate_and_publish_morning_edition(
            session,
            command=command,
        )


__all__ = [
    "DEFAULT_GENERATION_WINDOW",
    "MAX_GENERATION_SOURCE_ROWS",
    "MAX_GENERATION_WINDOW",
    "EditionGenerationCommand",
    "EditionGenerationError",
    "EditionGenerationSourceRowsExceeded",
    "EditionGenerationWindow",
    "build_generation_events_statement",
    "build_generation_watchlist_statement",
    "generate_and_publish_morning_edition",
    "load_generation_inputs",
    "run_morning_edition_generation",
]
