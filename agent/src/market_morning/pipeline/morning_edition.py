"""Deterministic, offline morning-edition generation core."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from urllib.parse import urlsplit


class EditionDayStatus(StrEnum):
    SCHEDULED = "scheduled"
    MARKET_HOLIDAY = "market_holiday"


class OvernightMarketContext(StrEnum):
    US_SESSION_AVAILABLE = "us_session_available"
    US_MARKET_CLOSED = "us_market_closed"


class EditionStatus(StrEnum):
    READY = "ready"
    PARTIAL = "partial"
    NO_DATA = "no_data"
    MARKET_HOLIDAY = "market_holiday"


class IssuerCollectionStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class IssuerBriefStatus(StrEnum):
    READY = "ready"
    PARTIAL = "partial"
    NO_CONFIRMED_EVENTS = "no_confirmed_events"
    INSUFFICIENT_DATA = "insufficient_data"
    UNAVAILABLE = "unavailable"


class StatementKind(StrEnum):
    FACT = "fact"
    INFERENCE = "inference"


def _required(value: str, *, field_name: str, max_length: int = 512) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field_name} must contain 1 to {max_length} characters")
    return normalized


def _aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


@dataclass(frozen=True, slots=True)
class EditionDayInput:
    edition_date: date
    jp_market_open: bool
    us_previous_session_available: bool
    jp_closure_reason: str | None = None
    us_closure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class EditionDayPlan:
    edition_date: date
    generate: bool
    status: EditionDayStatus
    overnight_context: OvernightMarketContext
    reason_code: str | None
    us_reason_code: str | None


@dataclass(frozen=True, slots=True)
class GenerationBudget:
    max_issuers: int = 10
    max_events_per_issuer: int = 5
    max_total_events: int = 30

    def __post_init__(self) -> None:
        limits = (
            ("max_issuers", self.max_issuers, 10),
            ("max_events_per_issuer", self.max_events_per_issuer, 10),
            ("max_total_events", self.max_total_events, 50),
        )
        for field_name, value, maximum in limits:
            if not 1 <= value <= maximum:
                raise ValueError(f"{field_name} must be between 1 and {maximum}")


@dataclass(frozen=True, slots=True)
class EditionSourceCitation:
    provider: str
    document_id: str
    revision_key: str
    original_url: str
    published_at: datetime

    def __post_init__(self) -> None:
        for field_name, maximum in (
            ("provider", 64),
            ("document_id", 191),
            ("revision_key", 128),
        ):
            object.__setattr__(
                self,
                field_name,
                _required(getattr(self, field_name), field_name=field_name, max_length=maximum),
            )
        url = urlsplit(self.original_url)
        if url.scheme != "https" or not url.hostname:
            raise ValueError("original_url must be an absolute HTTPS URL")
        _aware(self.published_at, field_name="published_at")


@dataclass(frozen=True, slots=True)
class EditionEventInput:
    event_id: str
    event_family_key: str
    event_version: int
    title: str
    event_type: str
    occurred_at: datetime
    lifecycle_status: str
    citations: tuple[EditionSourceCitation, ...]
    facts: tuple[EditionEventFactInput, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name, maximum in (
            ("event_id", 64),
            ("event_family_key", 191),
            ("title", 512),
            ("event_type", 64),
        ):
            object.__setattr__(
                self,
                field_name,
                _required(getattr(self, field_name), field_name=field_name, max_length=maximum),
            )
        if self.event_version < 1:
            raise ValueError("event_version must be positive")
        if self.lifecycle_status not in {"active", "corrected", "withdrawn"}:
            raise ValueError("lifecycle_status is unsupported")
        _aware(self.occurred_at, field_name="occurred_at")
        object.__setattr__(
            self,
            "warnings",
            tuple(
                _required(
                    warning,
                    field_name="event.warning",
                    max_length=64,
                )
                for warning in self.warnings
            ),
        )


@dataclass(frozen=True, slots=True)
class EditionEventFactInput:
    text: str
    citations: tuple[EditionSourceCitation, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "text",
            _required(self.text, field_name="fact.text", max_length=1_000),
        )
        if not self.citations:
            raise ValueError("fact.citations must not be empty")


@dataclass(frozen=True, slots=True)
class EditionIssuerInput:
    issuer_id: str
    issuer_code: str
    legal_name_ja: str
    collection_status: IssuerCollectionStatus | str
    events: tuple[EditionEventInput, ...]
    error_code: str | None = None

    def __post_init__(self) -> None:
        for field_name, maximum in (
            ("issuer_id", 64),
            ("issuer_code", 12),
            ("legal_name_ja", 255),
        ):
            object.__setattr__(
                self,
                field_name,
                _required(getattr(self, field_name), field_name=field_name, max_length=maximum),
            )
        try:
            status = IssuerCollectionStatus(self.collection_status)
        except ValueError as error:
            raise ValueError("collection_status is unsupported") from error
        object.__setattr__(self, "collection_status", status)
        if self.error_code is not None:
            object.__setattr__(
                self,
                "error_code",
                _required(self.error_code, field_name="error_code", max_length=64),
            )


@dataclass(frozen=True, slots=True)
class FactStatement:
    kind: StatementKind
    text: str
    event_id: str
    event_family_key: str
    event_version: int
    event_type: str
    occurred_at: datetime
    lifecycle_status: str
    citations: tuple[EditionSourceCitation, ...]


@dataclass(frozen=True, slots=True)
class InferenceStatement:
    kind: StatementKind = StatementKind.INFERENCE
    text: str = "不足以判断"
    evidence_quality: str = "insufficient_evidence"


@dataclass(frozen=True, slots=True)
class IssuerBrief:
    issuer_id: str
    issuer_code: str
    legal_name_ja: str
    status: IssuerBriefStatus
    facts: tuple[FactStatement, ...]
    assessment: InferenceStatement
    omitted_event_count: int
    warnings: tuple[str, ...]
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class MorningEdition:
    edition_date: date
    generated_at: datetime
    status: EditionStatus
    day_plan: EditionDayPlan
    issuers: tuple[IssuerBrief, ...]
    consumed_event_units: int
    budget: GenerationBudget
    budget_exhausted: bool
    omitted_issuer_count: int


def build_edition_day_plan(day: EditionDayInput) -> EditionDayPlan:
    overnight_context = (
        OvernightMarketContext.US_SESSION_AVAILABLE
        if day.us_previous_session_available
        else OvernightMarketContext.US_MARKET_CLOSED
    )
    if not day.jp_market_open:
        return EditionDayPlan(
            edition_date=day.edition_date,
            generate=False,
            status=EditionDayStatus.MARKET_HOLIDAY,
            overnight_context=overnight_context,
            reason_code=day.jp_closure_reason or "jp_market_closed",
            us_reason_code=day.us_closure_reason,
        )
    return EditionDayPlan(
        edition_date=day.edition_date,
        generate=True,
        status=EditionDayStatus.SCHEDULED,
        overnight_context=overnight_context,
        reason_code=None,
        us_reason_code=day.us_closure_reason,
    )


def _latest_event_revisions(
    events: tuple[EditionEventInput, ...],
) -> tuple[EditionEventInput, ...]:
    latest: dict[str, EditionEventInput] = {}
    for event in events:
        previous = latest.get(event.event_family_key)
        if previous is None or (
            event.event_version,
            event.occurred_at,
            event.event_id,
        ) > (
            previous.event_version,
            previous.occurred_at,
            previous.event_id,
        ):
            latest[event.event_family_key] = event
    return tuple(
        sorted(
            latest.values(),
            key=lambda item: (
                -item.occurred_at.timestamp(),
                item.event_family_key,
                item.event_id,
            ),
        )
    )


def _assessment() -> InferenceStatement:
    return InferenceStatement()


def _unavailable_brief(issuer: EditionIssuerInput) -> IssuerBrief:
    return IssuerBrief(
        issuer_id=issuer.issuer_id,
        issuer_code=issuer.issuer_code,
        legal_name_ja=issuer.legal_name_ja,
        status=IssuerBriefStatus.UNAVAILABLE,
        facts=(),
        assessment=_assessment(),
        omitted_event_count=len(issuer.events),
        warnings=("source_unavailable",),
        error_code=issuer.error_code or "source_unavailable",
    )


def generate_synthetic_edition(
    *,
    day_plan: EditionDayPlan,
    issuers: tuple[EditionIssuerInput, ...],
    generated_at: datetime,
    budget: GenerationBudget | None = None,
) -> MorningEdition:
    """Build an auditable edition without network, database, or LLM calls."""
    _aware(generated_at, field_name="generated_at")
    active_budget = budget or GenerationBudget()
    if not day_plan.generate:
        return MorningEdition(
            edition_date=day_plan.edition_date,
            generated_at=generated_at,
            status=EditionStatus.MARKET_HOLIDAY,
            day_plan=day_plan,
            issuers=(),
            consumed_event_units=0,
            budget=active_budget,
            budget_exhausted=False,
            omitted_issuer_count=0,
        )

    considered = issuers[: active_budget.max_issuers]
    omitted_issuer_count = max(0, len(issuers) - len(considered))
    remaining_event_units = active_budget.max_total_events
    consumed_event_units = 0
    budget_exhausted = omitted_issuer_count > 0
    briefs: list[IssuerBrief] = []

    for issuer in considered:
        if issuer.collection_status is IssuerCollectionStatus.UNAVAILABLE:
            briefs.append(_unavailable_brief(issuer))
            continue

        latest = _latest_event_revisions(issuer.events)
        cited = tuple(event for event in latest if event.citations)
        missing_citation_count = len(latest) - len(cited)
        issuer_event_limit = min(
            active_budget.max_events_per_issuer,
            remaining_event_units,
        )
        selected = cited[:issuer_event_limit]
        budget_omitted_count = len(cited) - len(selected)
        if budget_omitted_count:
            budget_exhausted = True
        remaining_event_units -= len(selected)
        consumed_event_units += len(selected)

        facts = tuple(
            FactStatement(
                kind=StatementKind.FACT,
                text=fact.text,
                event_id=event.event_id,
                event_family_key=event.event_family_key,
                event_version=event.event_version,
                event_type=event.event_type,
                occurred_at=event.occurred_at,
                lifecycle_status=event.lifecycle_status,
                citations=fact.citations,
            )
            for event in selected
            for fact in (
                event.facts
                or (
                    EditionEventFactInput(
                        text=event.title,
                        citations=event.citations,
                    ),
                )
            )
        )
        omitted_event_count = missing_citation_count + budget_omitted_count
        warnings: list[str] = []
        if missing_citation_count:
            warnings.append("missing_citation")
        if budget_omitted_count:
            warnings.append("generation_budget_exhausted")
        for event in selected:
            warnings.extend(event.warnings)
        warnings = list(dict.fromkeys(warnings))

        if issuer.collection_status is IssuerCollectionStatus.PARTIAL:
            status = IssuerBriefStatus.PARTIAL if facts else IssuerBriefStatus.INSUFFICIENT_DATA
            warnings.append("collection_incomplete")
        elif omitted_event_count or warnings:
            status = IssuerBriefStatus.PARTIAL
        elif facts:
            status = IssuerBriefStatus.READY
        else:
            status = IssuerBriefStatus.NO_CONFIRMED_EVENTS

        briefs.append(
            IssuerBrief(
                issuer_id=issuer.issuer_id,
                issuer_code=issuer.issuer_code,
                legal_name_ja=issuer.legal_name_ja,
                status=status,
                facts=facts,
                assessment=_assessment(),
                omitted_event_count=omitted_event_count,
                warnings=tuple(warnings),
                error_code=issuer.error_code,
            )
        )

    unavailable_statuses = {
        IssuerBriefStatus.UNAVAILABLE,
        IssuerBriefStatus.INSUFFICIENT_DATA,
    }
    if not briefs or all(brief.status in unavailable_statuses for brief in briefs):
        edition_status = EditionStatus.NO_DATA
    elif budget_exhausted or any(
        brief.status
        in {
            IssuerBriefStatus.PARTIAL,
            IssuerBriefStatus.UNAVAILABLE,
            IssuerBriefStatus.INSUFFICIENT_DATA,
        }
        for brief in briefs
    ):
        edition_status = EditionStatus.PARTIAL
    else:
        edition_status = EditionStatus.READY

    return MorningEdition(
        edition_date=day_plan.edition_date,
        generated_at=generated_at,
        status=edition_status,
        day_plan=day_plan,
        issuers=tuple(briefs),
        consumed_event_units=consumed_event_units,
        budget=active_budget,
        budget_exhausted=budget_exhausted,
        omitted_issuer_count=omitted_issuer_count,
    )


# The core is deterministic and has no synthetic-data dependency.  Keep the
# original name for the explicit demo path while exposing a production-safe
# name to persistence-backed orchestration.
generate_morning_edition = generate_synthetic_edition


__all__ = [
    "EditionDayInput",
    "EditionDayPlan",
    "EditionDayStatus",
    "EditionEventInput",
    "EditionIssuerInput",
    "EditionSourceCitation",
    "EditionStatus",
    "FactStatement",
    "GenerationBudget",
    "InferenceStatement",
    "IssuerBrief",
    "IssuerBriefStatus",
    "IssuerCollectionStatus",
    "MorningEdition",
    "OvernightMarketContext",
    "StatementKind",
    "build_edition_day_plan",
    "generate_morning_edition",
    "generate_synthetic_edition",
]
