"""Short-transaction orchestration for one versioned EventBrief generation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.market_morning.db import get_session_factory
from src.market_morning.event_brief_generation import EventBriefGenerationSpec
from src.market_morning.event_briefs import (
    EventBriefPublishStatus,
    EventBriefSource,
    build_blocked_event_brief,
    build_degraded_event_brief,
    parse_event_brief_v1,
    validate_event_brief,
)
from src.market_morning.models import EventSource, NormalizedEvent, SourceRecord
from src.market_morning.model_usage import (
    ModelUsageEvent,
    ModelUsageReport,
    build_model_usage_event,
)
from src.market_morning.repositories.event_briefs import (
    EventBriefPublishResult,
    EventBriefStartResult,
    fail_event_brief_generation,
    publish_event_brief,
    start_event_brief_generation,
)
from src.market_morning.repositories.model_usage import record_model_usage


class EventBriefGenerationError(RuntimeError):
    pass


class EventBriefGenerationRetryable(EventBriefGenerationError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class EventBriefGenerationUnavailable(EventBriefGenerationError):
    pass


class EventBriefModelUnavailable(RuntimeError):
    """Sanitized model-port failure; provider response must stay out of storage."""

    def __init__(
        self,
        message: str = "event_brief_model_unavailable",
        *,
        usage: ModelUsageReport | None = None,
    ) -> None:
        self.usage = usage
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class EventBriefSourceInput:
    source_id: str
    provider: str
    original_url: str
    evidence_text: str

    def __post_init__(self) -> None:
        for field_name, maximum in (
            ("source_id", 64),
            ("provider", 64),
            ("evidence_text", 50_000),
        ):
            value = getattr(self, field_name).strip()
            if not value or len(value) > maximum:
                raise ValueError(
                    f"{field_name} must contain 1 to {maximum} characters"
                )
            object.__setattr__(self, field_name, value)
        parsed = urlsplit(self.original_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("original_url must be an absolute HTTPS URL")


@dataclass(frozen=True, slots=True)
class EventBriefGenerationInput:
    event_id: str
    event_version: int
    title: str
    occurred_at: datetime
    lifecycle_status: str
    sources: tuple[EventBriefSourceInput, ...]

    def __post_init__(self) -> None:
        if self.lifecycle_status not in {"active", "corrected", "withdrawn"}:
            raise ValueError("event lifecycle_status is unsupported")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        if self.event_version < 1:
            raise ValueError("event_version must be positive")


@dataclass(frozen=True, slots=True)
class EventBriefModelInput:
    event_id: str
    event_version: int
    title: str
    occurred_at: datetime
    lifecycle_status: str
    sources: tuple[EventBriefSource, ...]


@dataclass(frozen=True, slots=True)
class EventBriefModelResponse:
    payload: dict[str, Any]
    usage: ModelUsageReport


@dataclass(frozen=True, slots=True)
class EventBriefGenerationCommand:
    spec: EventBriefGenerationSpec


@dataclass(frozen=True, slots=True)
class EventBriefGenerationResult:
    brief_id: str
    status: EventBriefPublishStatus
    attempt_number: int
    payload_sha256: str


class EventBriefGenerator(Protocol):
    async def __call__(
        self, model_input: EventBriefModelInput
    ) -> EventBriefModelResponse | dict[str, Any]: ...


SourceReachabilityChecker = Callable[[str], Awaitable[bool]]


def _utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _evidence_text(row: dict[str, Any]) -> str:
    payload = row.get("normalized_payload")
    if isinstance(payload, dict):
        candidate = payload.get("evidence_text")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return str(row["source_title"]).strip()


def build_event_brief_input_statement(spec: EventBriefGenerationSpec):
    return (
        select(
            NormalizedEvent.event_id,
            NormalizedEvent.event_version,
            NormalizedEvent.title.label("event_title"),
            NormalizedEvent.occurred_at,
            NormalizedEvent.lifecycle_status,
            SourceRecord.source_record_id,
            SourceRecord.source_provider,
            SourceRecord.original_url,
            SourceRecord.title.label("source_title"),
            SourceRecord.normalized_payload,
        )
        .select_from(NormalizedEvent)
        .outerjoin(EventSource, EventSource.event_id == NormalizedEvent.event_id)
        .outerjoin(
            SourceRecord,
            SourceRecord.source_record_id == EventSource.source_record_id,
        )
        .where(
            NormalizedEvent.event_id == spec.event_id,
            NormalizedEvent.event_version == spec.event_version,
        )
        .order_by(
            SourceRecord.source_provider,
            SourceRecord.provider_document_id,
            SourceRecord.provider_revision_key,
        )
    )


async def load_event_brief_generation_input(
    session: AsyncSession,
    *,
    spec: EventBriefGenerationSpec,
) -> EventBriefGenerationInput:
    rows = (
        await session.execute(build_event_brief_input_statement(spec))
    ).mappings().all()
    if not rows:
        raise EventBriefGenerationUnavailable("event_not_found")
    first = rows[0]
    sources: list[EventBriefSourceInput] = []
    seen: set[str] = set()
    for row in rows:
        source_id = row["source_record_id"]
        if source_id is None:
            continue
        if source_id in seen:
            continue
        seen.add(source_id)
        sources.append(
            EventBriefSourceInput(
                source_id=source_id,
                provider=row["source_provider"],
                original_url=row["original_url"],
                evidence_text=_evidence_text(row),
            )
        )
    if not sources:
        raise EventBriefGenerationUnavailable("event_sources_missing")
    return EventBriefGenerationInput(
        event_id=first["event_id"],
        event_version=first["event_version"],
        title=first["event_title"],
        occurred_at=_utc_aware(first["occurred_at"]),
        lifecycle_status=first["lifecycle_status"],
        sources=tuple(sources),
    )


async def _checked_sources(
    context: EventBriefGenerationInput,
    checker: SourceReachabilityChecker,
) -> tuple[EventBriefSource, ...]:
    reachable = await asyncio.gather(
        *(checker(source.original_url) for source in context.sources)
    )
    if any(type(value) is not bool for value in reachable):
        raise TypeError("reachability checker must return booleans")
    return tuple(
        EventBriefSource(
            source_id=source.source_id,
            provider=source.provider,
            original_url=source.original_url,
            reachable=is_reachable,
            evidence_text=source.evidence_text,
        )
        for source, is_reachable in zip(context.sources, reachable, strict=True)
    )


def _unchecked_sources(
    context: EventBriefGenerationInput,
) -> tuple[EventBriefSource, ...]:
    return tuple(
        EventBriefSource(
            source_id=source.source_id,
            provider=source.provider,
            original_url=source.original_url,
            reachable=False,
            evidence_text=source.evidence_text,
        )
        for source in context.sources
    )


async def _persist_terminal(
    *,
    factory,
    start: EventBriefStartResult,
    spec: EventBriefGenerationSpec,
    brief,
    completed_at: datetime,
    usage_event: ModelUsageEvent | None = None,
) -> EventBriefGenerationResult:
    async with factory.begin() as session:
        if usage_event is not None:
            await record_model_usage(session, event=usage_event)
        persisted: EventBriefPublishResult = await publish_event_brief(
            session,
            brief_id=start.brief_id,
            spec=spec,
            brief=brief,
            completed_at=completed_at,
        )
    return EventBriefGenerationResult(
        brief_id=persisted.brief_id,
        status=persisted.status,
        attempt_number=start.attempt_number,
        payload_sha256=persisted.payload_sha256,
    )


async def _record_retryable_failure(
    *,
    factory,
    start: EventBriefStartResult,
    spec: EventBriefGenerationSpec,
    error_code: str,
    failed_at: datetime,
    usage_event: ModelUsageEvent | None = None,
) -> None:
    async with factory.begin() as session:
        if usage_event is not None:
            await record_model_usage(session, event=usage_event)
        await fail_event_brief_generation(
            session,
            brief_id=start.brief_id,
            spec=spec,
            error_code=error_code,
            failed_at=failed_at,
        )
    raise EventBriefGenerationRetryable(error_code)


def _model_response(
    value: EventBriefModelResponse | dict[str, Any],
    *,
    model_version: str,
) -> EventBriefModelResponse:
    if isinstance(value, EventBriefModelResponse):
        return value
    if isinstance(value, dict):
        return EventBriefModelResponse(
            payload=value,
            usage=ModelUsageReport.missing(
                provider="unreported",
                model=model_version,
            ),
        )
    raise TypeError("event brief generator returned an unsupported response")


async def run_event_brief_generation(
    command: EventBriefGenerationCommand,
    *,
    generator: EventBriefGenerator,
    reachability_checker: SourceReachabilityChecker,
    session_factory: async_sessionmaker[AsyncSession] | Any | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> EventBriefGenerationResult:
    """Generate one event revision, retry transient ports, then degrade safely."""

    spec = command.spec
    factory = session_factory or get_session_factory()
    async with factory.begin() as session:
        start = await start_event_brief_generation(session, spec=spec)
        if not start.can_execute:
            if start.terminal_status is None:
                raise EventBriefGenerationUnavailable("event_brief_attempts_exhausted")
            return EventBriefGenerationResult(
                brief_id=start.brief_id,
                status=start.terminal_status,
                attempt_number=start.attempt_number,
                payload_sha256=start.payload_sha256 or "",
            )
        context = await load_event_brief_generation_input(session, spec=spec)

    try:
        sources = await _checked_sources(context, reachability_checker)
    except Exception:
        error_code = "event_brief_source_check_unavailable"
        if start.retry_permitted:
            await _record_retryable_failure(
                factory=factory,
                start=start,
                spec=spec,
                error_code=error_code,
                failed_at=clock(),
            )
        sources = _unchecked_sources(context)
        degraded = build_degraded_event_brief(
            event_id=context.event_id,
            event_version=context.event_version,
            title=context.title,
            occurred_at=context.occurred_at,
            sources=sources,
            error_code=error_code,
        )
        return await _persist_terminal(
            factory=factory,
            start=start,
            spec=spec,
            brief=degraded,
            completed_at=clock(),
        )

    if context.lifecycle_status == "withdrawn":
        degraded = build_degraded_event_brief(
            event_id=context.event_id,
            event_version=context.event_version,
            title=context.title,
            occurred_at=context.occurred_at,
            sources=sources,
            error_code="event_withdrawn",
        )
        return await _persist_terminal(
            factory=factory,
            start=start,
            spec=spec,
            brief=degraded,
            completed_at=clock(),
        )

    if any(not source.reachable for source in sources):
        blocked = build_blocked_event_brief(
            event_id=context.event_id,
            event_version=context.event_version,
            title=context.title,
            occurred_at=context.occurred_at,
            sources=sources,
            model_version=spec.model_version,
            error_codes=("source_unreachable",),
        )
        return await _persist_terminal(
            factory=factory,
            start=start,
            spec=spec,
            brief=blocked,
            completed_at=clock(),
        )

    try:
        response = _model_response(
            await generator(
                EventBriefModelInput(
                    event_id=context.event_id,
                    event_version=context.event_version,
                    title=context.title,
                    occurred_at=context.occurred_at,
                    lifecycle_status=context.lifecycle_status,
                    sources=sources,
                )
            ),
            model_version=spec.model_version,
        )
        model_completed_at = clock()
        usage_event = build_model_usage_event(
            brief_id=start.brief_id,
            attempt_number=start.attempt_number,
            report=response.usage,
            occurred_at=model_completed_at,
        )
        raw_payload = response.payload
    except Exception as error:
        error_code = "event_brief_model_unavailable"
        failed_at = clock()
        report = (
            error.usage
            if isinstance(error, EventBriefModelUnavailable)
            and error.usage is not None
            else ModelUsageReport.missing(
                provider="unknown",
                model=spec.model_version,
            )
        )
        usage_event = build_model_usage_event(
            brief_id=start.brief_id,
            attempt_number=start.attempt_number,
            report=report,
            occurred_at=failed_at,
        )
        if start.retry_permitted:
            await _record_retryable_failure(
                factory=factory,
                start=start,
                spec=spec,
                error_code=error_code,
                failed_at=failed_at,
                usage_event=usage_event,
            )
        degraded = build_degraded_event_brief(
            event_id=context.event_id,
            event_version=context.event_version,
            title=context.title,
            occurred_at=context.occurred_at,
            sources=sources,
            error_code=error_code,
        )
        return await _persist_terminal(
            factory=factory,
            start=start,
            spec=spec,
            brief=degraded,
            completed_at=failed_at,
            usage_event=usage_event,
        )

    errors: tuple[str, ...]
    try:
        draft = parse_event_brief_v1(raw_payload)
        if (
            draft.event_id != context.event_id
            or draft.event_version != context.event_version
            or draft.title != context.title
            or draft.occurred_at != context.occurred_at
            or draft.model_version != spec.model_version
        ):
            errors = ("event_brief_identity_mismatch",)
        else:
            validation = validate_event_brief(draft, sources=sources)
            if validation.status is EventBriefPublishStatus.PUBLISHED:
                return await _persist_terminal(
                    factory=factory,
                    start=start,
                    spec=spec,
                    brief=validation.brief,
                    completed_at=model_completed_at,
                    usage_event=usage_event,
                )
            errors = validation.error_codes
    except (TypeError, ValueError):
        errors = ("event_brief_schema_invalid",)

    blocked = build_blocked_event_brief(
        event_id=context.event_id,
        event_version=context.event_version,
        title=context.title,
        occurred_at=context.occurred_at,
        sources=sources,
        model_version=spec.model_version,
        error_codes=errors,
    )
    return await _persist_terminal(
        factory=factory,
        start=start,
        spec=spec,
        brief=blocked,
        completed_at=model_completed_at,
        usage_event=usage_event,
    )


__all__ = [
    "EventBriefGenerationCommand",
    "EventBriefGenerationError",
    "EventBriefGenerationInput",
    "EventBriefGenerationResult",
    "EventBriefGenerationRetryable",
    "EventBriefGenerationUnavailable",
    "EventBriefGenerator",
    "EventBriefModelInput",
    "EventBriefModelResponse",
    "EventBriefModelUnavailable",
    "EventBriefSourceInput",
    "SourceReachabilityChecker",
    "build_event_brief_input_statement",
    "load_event_brief_generation_input",
    "run_event_brief_generation",
]
