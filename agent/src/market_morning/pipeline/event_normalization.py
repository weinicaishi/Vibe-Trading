"""Deterministic event-family and cross-source merge-candidate rules."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from src.market_morning.sources.base import SourceValidationError

_TITLE_NOISE = re.compile(r"[\s\W_]+", flags=re.UNICODE)
_CORRECTION_MARKERS = ("訂正", "修正", "correction", "corrected")
_MERGE_WINDOW = timedelta(hours=24)
_SIMILARITY_THRESHOLD = 0.82


def _required(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise SourceValidationError(f"{field_name} is required")
    return normalized


def _aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SourceValidationError(f"{field_name} must be timezone-aware")
    return value


def normalize_event_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", _required(value, field_name="title")).lower()
    for marker in _CORRECTION_MARKERS:
        normalized = normalized.replace(marker, "")
    return _TITLE_NOISE.sub("", normalized)


def build_event_family_key(
    *,
    provider: str,
    provider_document_id: str,
    issuer_id: str,
) -> str:
    identity = "|".join(
        (
            _required(provider, field_name="provider"),
            _required(provider_document_id, field_name="provider_document_id"),
            _required(issuer_id, field_name="issuer_id"),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EventCandidateInput:
    event_id: str
    issuer_id: str
    source_provider: str
    title: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("event_id", "issuer_id", "source_provider", "title"):
            _required(getattr(self, field_name), field_name=field_name)
        _aware(self.occurred_at, field_name="occurred_at")


@dataclass(frozen=True, slots=True)
class MergeCandidateProposal:
    left_event_id: str
    right_event_id: str
    reason: str
    title_similarity: float
    time_distance_seconds: int
    review_status: str = "pending"
    auto_merge: bool = False


def propose_merge_candidate(
    left: EventCandidateInput,
    right: EventCandidateInput,
) -> MergeCandidateProposal | None:
    """Return a review proposal; this function never merges event identities."""
    if left.event_id == right.event_id:
        return None
    if left.issuer_id != right.issuer_id:
        return None
    if left.source_provider == right.source_provider:
        return None

    distance = abs(left.occurred_at - right.occurred_at)
    if distance > _MERGE_WINDOW:
        return None
    similarity = SequenceMatcher(
        None,
        normalize_event_title(left.title),
        normalize_event_title(right.title),
        autojunk=False,
    ).ratio()
    if similarity < _SIMILARITY_THRESHOLD:
        return None

    left_id, right_id = sorted((left.event_id, right.event_id))
    return MergeCandidateProposal(
        left_event_id=left_id,
        right_event_id=right_id,
        reason="same_issuer_similar_title_within_24h",
        title_similarity=similarity,
        time_distance_seconds=int(distance.total_seconds()),
    )


__all__ = [
    "EventCandidateInput",
    "MergeCandidateProposal",
    "build_event_family_key",
    "normalize_event_title",
    "propose_merge_candidate",
]
