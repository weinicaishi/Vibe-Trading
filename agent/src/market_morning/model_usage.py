"""Provider-neutral, integer-only model usage and cost contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import re
from typing import Any
from uuid import UUID

from src.market_morning.models import new_id


_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")


def _bounded(value: str, *, field_name: str, maximum: int) -> str:
    canonical = value.strip()
    if not canonical or len(canonical) > maximum:
        raise ValueError(f"{field_name} must contain 1 to {maximum} characters")
    return canonical


def _uuid(value: str, *, field_name: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise ValueError(f"{field_name} must be a UUID") from error


def _aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class ModelUsageReport:
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    billable_cost_micros: int | None = None
    currency: str | None = None
    provider_request_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider", _bounded(self.provider, field_name="provider", maximum=64)
        )
        object.__setattr__(
            self, "model", _bounded(self.model, field_name="model", maximum=128)
        )
        if (self.input_tokens is None) != (self.output_tokens is None):
            raise ValueError("input_tokens and output_tokens must both be provided")
        for field_name in ("input_tokens", "output_tokens", "billable_cost_micros"):
            value = getattr(self, field_name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.input_tokens is None and self.billable_cost_micros is not None:
            raise ValueError("cost cannot be reported when token usage is missing")
        if (self.billable_cost_micros is None) != (self.currency is None):
            raise ValueError("currency and billable_cost_micros must both be provided")
        if self.currency is not None:
            currency = self.currency.strip().upper()
            if not _CURRENCY_PATTERN.fullmatch(currency):
                raise ValueError("currency must be a three-letter ISO code")
            object.__setattr__(self, "currency", currency)
        if self.provider_request_id is not None:
            object.__setattr__(
                self,
                "provider_request_id",
                _bounded(
                    self.provider_request_id,
                    field_name="provider_request_id",
                    maximum=512,
                ),
            )

    @classmethod
    def missing(cls, *, provider: str, model: str) -> "ModelUsageReport":
        return cls(provider=provider, model=model)

    @property
    def usage_status(self) -> str:
        return "missing" if self.input_tokens is None else "reported"

    @property
    def cost_status(self) -> str:
        return "unpriced" if self.billable_cost_micros is None else "reported"

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ModelUsageEvent:
    usage_event_id: str
    usage_key: str
    brief_id: str
    attempt_number: int
    provider: str
    model: str
    usage_status: str
    cost_status: str
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    billable_cost_micros: int | None
    currency: str | None
    provider_request_id_sha256: str | None
    occurred_at: datetime

    def to_record_values(self) -> dict[str, Any]:
        occurred = self.occurred_at.astimezone(timezone.utc).replace(tzinfo=None)
        return {
            "usage_key": self.usage_key,
            "brief_id": self.brief_id,
            "attempt_number": self.attempt_number,
            "provider": self.provider,
            "model": self.model,
            "usage_status": self.usage_status,
            "cost_status": self.cost_status,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "billable_cost_micros": self.billable_cost_micros,
            "currency": self.currency,
            "provider_request_id_sha256": self.provider_request_id_sha256,
            "occurred_at": occurred,
            "created_at": occurred,
        }


def build_model_usage_event(
    *,
    brief_id: str,
    attempt_number: int,
    report: ModelUsageReport,
    occurred_at: datetime,
    usage_event_id: str | None = None,
) -> ModelUsageEvent:
    canonical_brief = _uuid(brief_id, field_name="brief_id")
    if isinstance(attempt_number, bool) or attempt_number < 1:
        raise ValueError("attempt_number must be a positive integer")
    request_digest = None
    if report.provider_request_id is not None:
        request_digest = hashlib.sha256(
            report.provider_request_id.encode("utf-8")
        ).hexdigest()
    return ModelUsageEvent(
        usage_event_id=_uuid(
            usage_event_id or new_id(), field_name="usage_event_id"
        ),
        usage_key=f"event-brief:{canonical_brief}:attempt:{attempt_number}",
        brief_id=canonical_brief,
        attempt_number=attempt_number,
        provider=report.provider,
        model=report.model,
        usage_status=report.usage_status,
        cost_status=report.cost_status,
        input_tokens=report.input_tokens,
        output_tokens=report.output_tokens,
        total_tokens=report.total_tokens,
        billable_cost_micros=report.billable_cost_micros,
        currency=report.currency,
        provider_request_id_sha256=request_digest,
        occurred_at=_aware(occurred_at, field_name="occurred_at"),
    )


__all__ = ["ModelUsageEvent", "ModelUsageReport", "build_model_usage_event"]
