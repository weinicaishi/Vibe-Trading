"""Privacy-safe first-party analytics for Market Morning.

Only explicitly registered event names and properties can be persisted. This
module intentionally has no catch-all metadata field: raw search text, email
addresses, private watchlist labels, notes, and source URLs must never enter
the analytics table.
"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.models import AnalyticsEvent, new_id, utc_now_naive


class AnalyticsValidationError(ValueError):
    """Raised before an unsafe or malformed analytics event is persisted."""


class AnalyticsEventName(StrEnum):
    SIGNUP_COMPLETED = "signup_completed"
    ISSUER_SEARCH_PERFORMED = "issuer_search_performed"
    WATCHLIST_ADDED = "watchlist_added"
    WATCHLIST_REMOVED = "watchlist_removed"
    WATCHLIST_UPDATED = "watchlist_updated"
    SETTINGS_UPDATED = "settings_updated"
    CONSENT_RECORDED = "consent_recorded"
    ACCOUNT_DELETION_REQUESTED = "account_deletion_requested"
    EDITION_OPENED = "edition_opened"
    SOURCE_OPENED = "source_opened"
    DELIVERY_CLICKED = "delivery_clicked"


_ALLOWED_PROPERTIES: dict[AnalyticsEventName, frozenset[str]] = {
    AnalyticsEventName.SIGNUP_COMPLETED: frozenset({"timezone"}),
    AnalyticsEventName.ISSUER_SEARCH_PERFORMED: frozenset(
        {"matched", "query_kind", "query_length", "result_count"}
    ),
    AnalyticsEventName.WATCHLIST_ADDED: frozenset(
        {"active_count", "operation_status"}
    ),
    AnalyticsEventName.WATCHLIST_REMOVED: frozenset(
        {"active_count", "operation_status"}
    ),
    AnalyticsEventName.WATCHLIST_UPDATED: frozenset(
        {"active_count", "operation_status"}
    ),
    AnalyticsEventName.SETTINGS_UPDATED: frozenset(
        {"email_opt_in", "timezone"}
    ),
    AnalyticsEventName.CONSENT_RECORDED: frozenset(
        {"consent_type", "consent_version", "status"}
    ),
    AnalyticsEventName.ACCOUNT_DELETION_REQUESTED: frozenset(
        {"request_status"}
    ),
    AnalyticsEventName.EDITION_OPENED: frozenset({"edition_date"}),
    AnalyticsEventName.SOURCE_OPENED: frozenset({"source_kind"}),
    AnalyticsEventName.DELIVERY_CLICKED: frozenset({"channel"}),
}

_ENUM_PROPERTIES: dict[tuple[AnalyticsEventName, str], frozenset[str]] = {
    (AnalyticsEventName.SIGNUP_COMPLETED, "timezone"): frozenset({"Asia/Tokyo"}),
    (AnalyticsEventName.ISSUER_SEARCH_PERFORMED, "query_kind"): frozenset(
        {"issuer_code", "official_name", "alias", "other"}
    ),
    (AnalyticsEventName.WATCHLIST_ADDED, "operation_status"): frozenset({"added"}),
    (AnalyticsEventName.WATCHLIST_REMOVED, "operation_status"): frozenset(
        {"removed"}
    ),
    (AnalyticsEventName.WATCHLIST_UPDATED, "operation_status"): frozenset(
        {"updated"}
    ),
    (AnalyticsEventName.SETTINGS_UPDATED, "timezone"): frozenset({"Asia/Tokyo"}),
    (AnalyticsEventName.CONSENT_RECORDED, "consent_type"): frozenset(
        {"risk_disclosure", "data_disclosure"}
    ),
    (AnalyticsEventName.CONSENT_RECORDED, "status"): frozenset(
        {"accepted", "revoked"}
    ),
    (AnalyticsEventName.ACCOUNT_DELETION_REQUESTED, "request_status"): frozenset(
        {"pending"}
    ),
    (AnalyticsEventName.SOURCE_OPENED, "source_kind"): frozenset(
        {"tdnet", "edinet", "issuer_ir", "market_data", "licensed_news", "other"}
    ),
    (AnalyticsEventName.DELIVERY_CLICKED, "channel"): frozenset({"email", "web"}),
}

_TOKEN_PROPERTIES = {
    (AnalyticsEventName.CONSENT_RECORDED, "consent_version"),
    (AnalyticsEventName.EDITION_OPENED, "edition_date"),
}
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9._:-]+")


def _canonical_uuid(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise AnalyticsValidationError(f"{field} must be a UUID") from exc


def _safe_short_string(value: str, *, field: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized or len(normalized) > 64:
        raise AnalyticsValidationError(
            f"analytics property {field} must contain 1 to 64 characters"
        )
    if any(unicodedata.category(char).startswith("C") for char in normalized):
        raise AnalyticsValidationError(
            f"analytics property {field} contains unsupported control characters"
        )
    return normalized


def _safe_property(value: Any, *, field: str) -> bool | int | float | str:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > 1_000_000_000:
            raise AnalyticsValidationError(f"analytics property {field} is out of range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) > 1_000_000_000:
            raise AnalyticsValidationError(f"analytics property {field} is out of range")
        return value
    if isinstance(value, str):
        return _safe_short_string(value, field=field)
    raise AnalyticsValidationError(
        f"analytics property {field} must be a scalar boolean, number, or short string"
    )


def _safe_request_id(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _safe_short_string(value, field="request_id")
    if _SAFE_TOKEN.fullmatch(normalized) is None:
        raise AnalyticsValidationError("request_id contains unsupported characters")
    return normalized


def _validated_property(
    event_name: AnalyticsEventName, key: str, value: Any
) -> bool | int | float | str:
    safe_value = _safe_property(value, field=key)
    allowed_values = _ENUM_PROPERTIES.get((event_name, key))
    if allowed_values is not None and safe_value not in allowed_values:
        raise AnalyticsValidationError(
            f"analytics property {key} is not allowed for {event_name.value}"
        )
    if (event_name, key) in _TOKEN_PROPERTIES:
        if not isinstance(safe_value, str) or _SAFE_TOKEN.fullmatch(safe_value) is None:
            raise AnalyticsValidationError(
                f"analytics property {key} must be a safe identifier"
            )
    if key in {"active_count", "query_length", "result_count"}:
        if isinstance(safe_value, bool) or not isinstance(safe_value, int) or safe_value < 0:
            raise AnalyticsValidationError(
                f"analytics property {key} must be a non-negative integer"
            )
    return safe_value


def build_analytics_event(
    event_name: AnalyticsEventName | str,
    *,
    user_id: str | None,
    properties: dict[str, Any] | None = None,
    request_id: str | None = None,
    occurred_at: datetime | None = None,
) -> AnalyticsEvent:
    """Build an event after applying the event-specific privacy allowlist."""
    try:
        canonical_name = AnalyticsEventName(event_name)
    except ValueError as exc:
        raise AnalyticsValidationError("analytics event name is not registered") from exc

    supplied = properties or {}
    unexpected = set(supplied) - _ALLOWED_PROPERTIES[canonical_name]
    if unexpected:
        names = ", ".join(sorted(unexpected))
        raise AnalyticsValidationError(
            f"analytics event {canonical_name.value} contains disallowed properties: {names}"
        )
    safe_properties = {
        key: _validated_property(canonical_name, key, value)
        for key, value in supplied.items()
    }
    return AnalyticsEvent(
        event_id=new_id(),
        event_name=canonical_name.value,
        user_id=_canonical_uuid(user_id, field="user_id"),
        request_id=_safe_request_id(request_id),
        schema_version=1,
        properties=safe_properties,
        occurred_at=occurred_at or utc_now_naive(),
    )


def append_analytics_event(
    session: AsyncSession,
    event_name: AnalyticsEventName | str,
    *,
    user_id: str | None,
    properties: dict[str, Any] | None = None,
    request_id: str | None = None,
    occurred_at: datetime | None = None,
) -> AnalyticsEvent:
    """Validate and append an analytics event to the caller's transaction."""
    event = build_analytics_event(
        event_name,
        user_id=user_id,
        properties=properties,
        request_id=request_id,
        occurred_at=occurred_at,
    )
    session.add(event)
    return event


__all__ = [
    "AnalyticsEventName",
    "AnalyticsValidationError",
    "append_analytics_event",
    "build_analytics_event",
]
