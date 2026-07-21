"""Versioned EventBrief schema and deterministic publication quality Gate."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

EVENT_BRIEF_SCHEMA_VERSION = 1

_NUMBER_PATTERN = re.compile(r"\d[\d,.]*(?:%|％)?")
_PROHIBITED_RECOMMENDATION_PHRASES = (
    "今すぐ買う",
    "買うべき",
    "売るべき",
    "買い推奨",
    "売り推奨",
    "必ず上がる",
    "強烈推荐",
    "买入该股票",
    "稳赚",
    "guaranteed profit",
    "buy now",
    "strong buy",
    "sell now",
)


class EventBriefReviewStatus(StrEnum):
    PENDING = "pending"
    AUTO_VALIDATED = "auto_validated"
    APPROVED = "approved"
    REJECTED = "rejected"


class EventBriefPublishStatus(StrEnum):
    PUBLISHED = "published"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


def _required(value: str, *, field_name: str, maximum: int) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field_name} must contain 1 to {maximum} characters")
    if any(unicodedata.category(char).startswith("C") for char in normalized):
        raise ValueError(f"{field_name} contains control characters")
    return normalized


def _aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _unique_ids(values: tuple[str, ...], *, field_name: str) -> tuple[str, ...]:
    normalized = tuple(
        _required(value, field_name=field_name, maximum=64) for value in values
    )
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} must not contain duplicates")
    return normalized


@dataclass(frozen=True, slots=True)
class ConfirmedFact:
    text: str
    source_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "text",
            _required(self.text, field_name="fact.text", maximum=1_000),
        )
        object.__setattr__(
            self,
            "source_ids",
            _unique_ids(self.source_ids, field_name="fact.source_ids"),
        )


@dataclass(frozen=True, slots=True)
class EventBriefDraft:
    schema_version: int
    event_id: str
    event_version: int
    title: str
    occurred_at: datetime
    confirmed_facts: tuple[ConfirmedFact, ...]
    open_questions: tuple[str, ...]
    source_ids: tuple[str, ...]
    model_version: str
    review_status: EventBriefReviewStatus

    def __post_init__(self) -> None:
        if self.schema_version != EVENT_BRIEF_SCHEMA_VERSION:
            raise ValueError("event brief schema_version is unsupported")
        object.__setattr__(
            self,
            "event_id",
            _required(self.event_id, field_name="event_id", maximum=64),
        )
        if isinstance(self.event_version, bool) or self.event_version < 1:
            raise ValueError("event_version must be a positive integer")
        object.__setattr__(
            self,
            "title",
            _required(self.title, field_name="title", maximum=512),
        )
        _aware(self.occurred_at, field_name="occurred_at")
        questions = tuple(
            _required(question, field_name="open_question", maximum=1_000)
            for question in self.open_questions
        )
        if len(questions) > 10 or len(self.confirmed_facts) > 20:
            raise ValueError("event brief content exceeds schema limits")
        if not self.confirmed_facts and not questions:
            raise ValueError("event brief must contain a fact or open question")
        object.__setattr__(self, "open_questions", questions)
        object.__setattr__(
            self,
            "source_ids",
            _unique_ids(self.source_ids, field_name="source_ids"),
        )
        object.__setattr__(
            self,
            "model_version",
            _required(
                self.model_version,
                field_name="model_version",
                maximum=128,
            ),
        )
        object.__setattr__(
            self,
            "review_status",
            EventBriefReviewStatus(self.review_status),
        )


@dataclass(frozen=True, slots=True)
class EventBriefSource:
    source_id: str
    provider: str
    original_url: str
    reachable: bool
    evidence_text: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_id",
            _required(self.source_id, field_name="source_id", maximum=64),
        )
        object.__setattr__(
            self,
            "provider",
            _required(self.provider, field_name="provider", maximum=64),
        )
        parsed = urlsplit(self.original_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("original_url must be an absolute HTTPS URL")
        if type(self.reachable) is not bool:
            raise TypeError("reachable must be a boolean")
        object.__setattr__(
            self,
            "evidence_text",
            _required(
                self.evidence_text,
                field_name="evidence_text",
                maximum=50_000,
            ),
        )


@dataclass(frozen=True, slots=True)
class EventBrief:
    schema_version: int
    status: EventBriefPublishStatus
    event_id: str
    event_version: int
    title: str
    occurred_at: datetime
    confirmed_facts: tuple[ConfirmedFact, ...]
    open_questions: tuple[str, ...]
    source_ids: tuple[str, ...]
    sources: tuple[EventBriefSource, ...]
    model_version: str | None
    review_status: EventBriefReviewStatus
    error_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EventBriefValidationResult:
    status: EventBriefPublishStatus
    brief: EventBrief
    error_codes: tuple[str, ...]


def _strict_object(
    value: Any,
    *,
    field_name: str,
    required: frozenset[str],
) -> dict[str, Any]:
    if not isinstance(value, dict) or frozenset(value) != required:
        raise ValueError(f"{field_name} has invalid fields")
    return value


def _string_list(value: Any, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError(f"{field_name} must be a string array")
    return tuple(value)


def parse_event_brief_v1(payload: dict[str, Any]) -> EventBriefDraft:
    """Parse strict model JSON; unknown fields and implicit coercions are rejected."""

    root = _strict_object(
        payload,
        field_name="event_brief",
        required=frozenset(
            {
                "schema_version",
                "event_id",
                "event_version",
                "title",
                "occurred_at",
                "confirmed_facts",
                "open_questions",
                "source_ids",
                "model_version",
                "review_status",
            }
        ),
    )
    if not isinstance(root["schema_version"], int):
        raise TypeError("schema_version must be an integer")
    if not isinstance(root["event_version"], int):
        raise TypeError("event_version must be an integer")
    if not isinstance(root["occurred_at"], str):
        raise TypeError("occurred_at must be a string")
    if not isinstance(root["confirmed_facts"], list):
        raise TypeError("confirmed_facts must be an array")
    facts: list[ConfirmedFact] = []
    for raw_fact in root["confirmed_facts"]:
        fact = _strict_object(
            raw_fact,
            field_name="confirmed_fact",
            required=frozenset({"text", "source_ids"}),
        )
        if not isinstance(fact["text"], str):
            raise TypeError("confirmed_fact.text must be a string")
        facts.append(
            ConfirmedFact(
                text=fact["text"],
                source_ids=_string_list(
                    fact["source_ids"],
                    field_name="confirmed_fact.source_ids",
                ),
            )
        )
    string_fields = ("event_id", "title", "model_version", "review_status")
    if any(not isinstance(root[field], str) for field in string_fields):
        raise TypeError("event brief string field has an invalid type")
    return EventBriefDraft(
        schema_version=root["schema_version"],
        event_id=root["event_id"],
        event_version=root["event_version"],
        title=root["title"],
        occurred_at=datetime.fromisoformat(root["occurred_at"]),
        confirmed_facts=tuple(facts),
        open_questions=_string_list(
            root["open_questions"],
            field_name="open_questions",
        ),
        source_ids=_string_list(root["source_ids"], field_name="source_ids"),
        model_version=root["model_version"],
        review_status=EventBriefReviewStatus(root["review_status"]),
    )


def _normalized_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _contains_prohibited_language(value: str) -> bool:
    normalized = _normalized_text(value)
    return any(
        _normalized_text(phrase) in normalized
        for phrase in _PROHIBITED_RECOMMENDATION_PHRASES
    )


def _numeric_tokens(value: str) -> frozenset[str]:
    tokens: set[str] = set()
    for match in _NUMBER_PATTERN.findall(unicodedata.normalize("NFKC", value)):
        token = match.replace(",", "").replace("％", "%")
        tokens.add(token)
    return frozenset(tokens)


def _brief_from_draft(
    draft: EventBriefDraft,
    *,
    status: EventBriefPublishStatus,
    sources: tuple[EventBriefSource, ...],
    error_codes: tuple[str, ...],
) -> EventBrief:
    review_status = (
        EventBriefReviewStatus.AUTO_VALIDATED
        if status is EventBriefPublishStatus.PUBLISHED
        else draft.review_status
    )
    return EventBrief(
        schema_version=draft.schema_version,
        status=status,
        event_id=draft.event_id,
        event_version=draft.event_version,
        title=draft.title,
        occurred_at=draft.occurred_at,
        confirmed_facts=draft.confirmed_facts,
        open_questions=draft.open_questions,
        source_ids=draft.source_ids,
        sources=sources,
        model_version=draft.model_version,
        review_status=review_status,
        error_codes=error_codes,
    )


def validate_event_brief(
    draft: EventBriefDraft,
    *,
    sources: tuple[EventBriefSource, ...],
) -> EventBriefValidationResult:
    """Fail closed when citations or generated claims cannot be verified."""

    errors: list[str] = []
    source_by_id: dict[str, EventBriefSource] = {}
    for source in sources:
        if source.source_id in source_by_id:
            errors.append("source_duplicate")
        source_by_id[source.source_id] = source

    declared_sources: list[EventBriefSource] = []
    for source_id in draft.source_ids:
        source = source_by_id.get(source_id)
        if source is None:
            errors.append("source_missing")
            continue
        declared_sources.append(source)
        if not source.reachable:
            errors.append("source_unreachable")
    if not draft.source_ids:
        errors.append("source_ids_empty")

    for fact in draft.confirmed_facts:
        if not fact.source_ids:
            errors.append("fact_source_ids_empty")
        evidence_parts: list[str] = []
        for source_id in fact.source_ids:
            if source_id not in draft.source_ids:
                errors.append("fact_source_not_declared")
            source = source_by_id.get(source_id)
            if source is None:
                errors.append("source_missing")
            elif not source.reachable:
                errors.append("source_unreachable")
            else:
                evidence_parts.append(source.evidence_text)
        if _contains_prohibited_language(fact.text):
            errors.append("prohibited_recommendation_language")
        claimed_tokens = _numeric_tokens(fact.text)
        evidence_tokens = _numeric_tokens(" ".join(evidence_parts))
        if not claimed_tokens <= evidence_tokens:
            errors.append("unsupported_numeric_or_date_claim")

    all_evidence = " ".join(source.evidence_text for source in declared_sources)
    all_evidence_tokens = _numeric_tokens(all_evidence)
    for question in draft.open_questions:
        if _contains_prohibited_language(question):
            errors.append("prohibited_recommendation_language")
        if not _numeric_tokens(question) <= all_evidence_tokens:
            errors.append("unsupported_numeric_or_date_claim")

    error_codes = tuple(dict.fromkeys(errors))
    status = (
        EventBriefPublishStatus.BLOCKED
        if error_codes
        else EventBriefPublishStatus.PUBLISHED
    )
    brief = _brief_from_draft(
        draft,
        status=status,
        sources=tuple(declared_sources),
        error_codes=error_codes,
    )
    return EventBriefValidationResult(
        status=status,
        brief=brief,
        error_codes=error_codes,
    )


def build_degraded_event_brief(
    *,
    event_id: str,
    event_version: int,
    title: str,
    occurred_at: datetime,
    sources: tuple[EventBriefSource, ...],
    error_code: str,
) -> EventBrief:
    """Build the non-model fallback card: identity, time and source links only."""

    identity = EventBriefDraft(
        schema_version=EVENT_BRIEF_SCHEMA_VERSION,
        event_id=event_id,
        event_version=event_version,
        title=title,
        occurred_at=occurred_at,
        confirmed_facts=(),
        open_questions=("fallback",),
        source_ids=tuple(source.source_id for source in sources),
        model_version="fallback",
        review_status=EventBriefReviewStatus.PENDING,
    )
    brief = _brief_from_draft(
        identity,
        status=EventBriefPublishStatus.DEGRADED,
        sources=sources,
        error_codes=(
            _required(error_code, field_name="error_code", maximum=64),
        ),
    )
    return replace(
        brief,
        confirmed_facts=(),
        open_questions=(),
        model_version=None,
    )


def build_blocked_event_brief(
    *,
    event_id: str,
    event_version: int,
    title: str,
    occurred_at: datetime,
    sources: tuple[EventBriefSource, ...],
    model_version: str,
    error_codes: tuple[str, ...],
) -> EventBrief:
    """Build an audit-safe blocked record without retaining invalid model claims."""

    canonical_errors = tuple(
        _required(error, field_name="error_code", maximum=64)
        for error in dict.fromkeys(error_codes)
    )
    if not canonical_errors:
        raise ValueError("blocked event brief must contain an error code")
    return EventBrief(
        schema_version=EVENT_BRIEF_SCHEMA_VERSION,
        status=EventBriefPublishStatus.BLOCKED,
        event_id=_required(event_id, field_name="event_id", maximum=64),
        event_version=event_version,
        title=_required(title, field_name="title", maximum=512),
        occurred_at=_aware(occurred_at, field_name="occurred_at"),
        confirmed_facts=(),
        open_questions=(),
        source_ids=tuple(source.source_id for source in sources),
        sources=sources,
        model_version=_required(
            model_version,
            field_name="model_version",
            maximum=128,
        ),
        review_status=EventBriefReviewStatus.PENDING,
        error_codes=canonical_errors,
    )


def encode_event_brief(brief: EventBrief) -> dict[str, Any]:
    """Return the immutable persistence/read-model payload without evidence text."""

    return {
        "schema_version": brief.schema_version,
        "status": brief.status.value,
        "event_id": brief.event_id,
        "event_version": brief.event_version,
        "title": brief.title,
        "occurred_at": brief.occurred_at.astimezone(timezone.utc).isoformat(),
        "confirmed_facts": [
            {
                "text": fact.text,
                "source_ids": list(fact.source_ids),
            }
            for fact in brief.confirmed_facts
        ],
        "open_questions": list(brief.open_questions),
        "source_ids": list(brief.source_ids),
        "sources": [
            {
                "source_id": source.source_id,
                "provider": source.provider,
                "original_url": source.original_url,
            }
            for source in brief.sources
        ],
        "model_version": brief.model_version,
        "review_status": brief.review_status.value,
        "error_codes": list(brief.error_codes),
    }


__all__ = [
    "EVENT_BRIEF_SCHEMA_VERSION",
    "ConfirmedFact",
    "EventBrief",
    "EventBriefDraft",
    "EventBriefPublishStatus",
    "EventBriefReviewStatus",
    "EventBriefSource",
    "EventBriefValidationResult",
    "build_degraded_event_brief",
    "build_blocked_event_brief",
    "encode_event_brief",
    "parse_event_brief_v1",
    "validate_event_brief",
]
