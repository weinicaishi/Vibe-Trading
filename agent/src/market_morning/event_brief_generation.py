"""Stable identity and hashing for one event-revision brief generation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from src.market_morning.event_briefs import EVENT_BRIEF_SCHEMA_VERSION


def _uuid(value: str, *, field_name: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise ValueError(f"{field_name} must be a UUID") from error


def _required(value: str, *, field_name: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field_name} must contain 1 to {maximum} characters")
    return normalized


def _aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


@dataclass(frozen=True, slots=True)
class EventBriefGenerationSpec:
    event_id: str
    event_version: int
    generation_key: str
    model_version: str
    prompt_version: str
    started_at: datetime
    max_attempts: int = 3
    schema_version: int = EVENT_BRIEF_SCHEMA_VERSION

    def __post_init__(self) -> None:
        event_id = _uuid(self.event_id, field_name="event_id")
        object.__setattr__(self, "event_id", event_id)
        if isinstance(self.event_version, bool) or self.event_version < 1:
            raise ValueError("event_version must be a positive integer")
        if self.schema_version != EVENT_BRIEF_SCHEMA_VERSION:
            raise ValueError("schema_version is unsupported")
        model_version = _required(
            self.model_version,
            field_name="model_version",
            maximum=64,
        )
        prompt_version = _required(
            self.prompt_version,
            field_name="prompt_version",
            maximum=64,
        )
        object.__setattr__(self, "model_version", model_version)
        object.__setattr__(self, "prompt_version", prompt_version)
        expected_key = (
            f"event-brief:{event_id}:v{self.event_version}:"
            f"{model_version}:{prompt_version}:s{self.schema_version}"
        )
        if self.generation_key != expected_key or len(expected_key) > 191:
            raise ValueError("generation_key does not match the generation spec")
        _aware(self.started_at, field_name="started_at")
        if not 1 <= self.max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")


def encode_event_brief_generation_spec(
    spec: EventBriefGenerationSpec,
) -> dict[str, Any]:
    return {
        "schema_version": spec.schema_version,
        "event_id": spec.event_id,
        "event_version": spec.event_version,
        "generation_key": spec.generation_key,
        "model_version": spec.model_version,
        "prompt_version": spec.prompt_version,
        "max_attempts": spec.max_attempts,
        "started_at": spec.started_at.astimezone(timezone.utc).isoformat(),
    }


def event_brief_generation_spec_sha256(spec: EventBriefGenerationSpec) -> str:
    encoded = json.dumps(
        encode_event_brief_generation_spec(spec),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "EventBriefGenerationSpec",
    "encode_event_brief_generation_spec",
    "event_brief_generation_spec_sha256",
]
