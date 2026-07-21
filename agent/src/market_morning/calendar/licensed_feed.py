"""Preloaded, licensed-provider-neutral trading calendars.

The runtime calendar protocol is synchronous because scheduler decisions must
not perform network I/O while a database lease transaction is open.  This
module therefore loads and validates a complete calendar manifest before the
runtime starts, then exposes an immutable in-memory calendar.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from src.market_morning.calendar.service import (
    MAX_SESSION_SEARCH_DAYS,
    CalendarUnavailable,
    MarketCode,
    MarketSession,
)
from src.market_morning.http_reachability import AddressResolver, resolve_host_addresses
from src.market_morning.licensed_http import (
    ApprovedHttpsEndpoint,
    HeaderFactory,
    LicensedHttpError,
    fetch_approved_https_bytes,
)

CalendarManifestParser = Callable[
    [bytes, str, "LicensedCalendarFeedConfig"],
    "LicensedCalendarManifest",
]


def _required(value: Any, *, error_code: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(error_code)
    canonical = " ".join(value.split())
    if (
        not canonical
        or len(canonical) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in canonical)
    ):
        raise ValueError(error_code)
    return canonical


def _strict_date(value: Any, *, error_code: str) -> date:
    if not isinstance(value, str):
        raise ValueError(error_code)
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(error_code) from None
    if parsed.isoformat() != value:
        raise ValueError(error_code)
    return parsed


def _aware_datetime(value: Any, *, error_code: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(error_code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(error_code) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(error_code)
    return parsed


@dataclass(frozen=True, slots=True)
class LicensedCalendarFeedConfig:
    """Deployment-reviewed endpoint and freshness policy for one market."""

    provider: str
    market: MarketCode | str
    endpoint_url: str = field(repr=False)
    allowed_base_urls: tuple[str, ...]
    header_factory: HeaderFactory | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    max_manifest_age: timedelta = timedelta(days=7)
    max_future_skew: timedelta = timedelta(minutes=5)
    timeout_seconds: float = 10.0
    max_response_bytes: int = 2_000_000
    _endpoint: ApprovedHttpsEndpoint = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        provider = _required(
            self.provider,
            error_code="licensed_calendar_provider_invalid",
            maximum=64,
        )
        if provider.startswith("fixture_"):
            raise ValueError("licensed_calendar_provider_invalid")
        try:
            market = MarketCode(self.market)
        except ValueError:
            raise ValueError("licensed_calendar_market_invalid") from None
        if not timedelta(hours=1) <= self.max_manifest_age <= timedelta(days=30):
            raise ValueError("licensed_calendar_max_age_invalid")
        if not timedelta(0) <= self.max_future_skew <= timedelta(minutes=30):
            raise ValueError("licensed_calendar_future_skew_invalid")
        endpoint = ApprovedHttpsEndpoint(
            url=self.endpoint_url,
            allowed_base_urls=self.allowed_base_urls,
            accepted_media_types=("application/json",),
            max_response_bytes=self.max_response_bytes,
        )
        if not isinstance(self.timeout_seconds, (int, float)) or not (
            0.05 <= float(self.timeout_seconds) <= 30.0
        ):
            raise ValueError("licensed_calendar_timeout_invalid")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "market", market)
        object.__setattr__(self, "endpoint_url", endpoint.url)
        object.__setattr__(self, "allowed_base_urls", endpoint.allowed_base_urls)
        object.__setattr__(self, "_endpoint", endpoint)

    @property
    def endpoint(self) -> ApprovedHttpsEndpoint:
        return self._endpoint


@dataclass(frozen=True, slots=True)
class LicensedCalendarManifest:
    provider: str
    market: MarketCode
    generated_at: datetime
    coverage_start: date
    coverage_end: date
    sessions: tuple[MarketSession, ...]

    def __post_init__(self) -> None:
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("licensed_calendar_manifest_invalid")
        if self.coverage_end < self.coverage_start:
            raise ValueError("licensed_calendar_manifest_invalid")
        expected_count = (self.coverage_end - self.coverage_start).days + 1
        if not 1 <= expected_count <= 2_000 or len(self.sessions) != expected_count:
            raise ValueError("licensed_calendar_manifest_invalid")
        for offset, session in enumerate(self.sessions):
            if (
                not isinstance(session, MarketSession)
                or session.provider != self.provider
                or session.market is not self.market
                or session.session_date
                != self.coverage_start + timedelta(days=offset)
            ):
                raise ValueError("licensed_calendar_manifest_invalid")


def parse_licensed_calendar_json(
    content: bytes,
    media_type: str,
    config: LicensedCalendarFeedConfig,
) -> LicensedCalendarManifest:
    """Parse the strict normalized manifest supported by the core adapter."""

    if media_type != "application/json":
        raise ValueError("licensed_calendar_contract_invalid")
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("licensed_calendar_contract_invalid") from None
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "provider",
        "market",
        "generated_at",
        "coverage_start",
        "coverage_end",
        "sessions",
    }:
        raise ValueError("licensed_calendar_contract_invalid")
    if payload["schema_version"] != 1 or isinstance(payload["schema_version"], bool):
        raise ValueError("licensed_calendar_contract_invalid")
    provider = _required(
        payload["provider"],
        error_code="licensed_calendar_contract_invalid",
        maximum=64,
    )
    if provider != config.provider or payload["market"] != config.market.value:
        raise ValueError("licensed_calendar_contract_invalid")
    generated_at = _aware_datetime(
        payload["generated_at"],
        error_code="licensed_calendar_contract_invalid",
    )
    coverage_start = _strict_date(
        payload["coverage_start"],
        error_code="licensed_calendar_contract_invalid",
    )
    coverage_end = _strict_date(
        payload["coverage_end"],
        error_code="licensed_calendar_contract_invalid",
    )
    raw_sessions = payload["sessions"]
    if not isinstance(raw_sessions, list) or len(raw_sessions) > 2_000:
        raise ValueError("licensed_calendar_contract_invalid")
    sessions: list[MarketSession] = []
    for raw in raw_sessions:
        if not isinstance(raw, dict) or set(raw) != {
            "session_date",
            "is_open",
            "reason_code",
        }:
            raise ValueError("licensed_calendar_contract_invalid")
        if type(raw["is_open"]) is not bool:
            raise ValueError("licensed_calendar_contract_invalid")
        reason = raw["reason_code"]
        if reason is not None:
            reason = _required(
                reason,
                error_code="licensed_calendar_contract_invalid",
                maximum=64,
            )
        try:
            session = MarketSession(
                market=config.market,
                session_date=_strict_date(
                    raw["session_date"],
                    error_code="licensed_calendar_contract_invalid",
                ),
                is_open=raw["is_open"],
                reason_code=reason,
                provider=config.provider,
            )
        except (TypeError, ValueError):
            raise ValueError("licensed_calendar_contract_invalid") from None
        sessions.append(session)
    return LicensedCalendarManifest(
        provider=provider,
        market=config.market,
        generated_at=generated_at,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        sessions=tuple(sessions),
    )


class LicensedTradingCalendar:
    """Immutable synchronous calendar built from a validated manifest."""

    def __init__(self, manifest: LicensedCalendarManifest) -> None:
        self.provider = manifest.provider
        self.market = manifest.market
        self.generated_at = manifest.generated_at
        self.coverage_start = manifest.coverage_start
        self.coverage_end = manifest.coverage_end
        self._sessions = {
            session.session_date: session for session in manifest.sessions
        }

    def session_on(self, session_date: date) -> MarketSession:
        if type(session_date) is not date:
            raise TypeError("session_date must be a date")
        try:
            return self._sessions[session_date]
        except KeyError:
            raise CalendarUnavailable("licensed_calendar_out_of_coverage") from None

    def _open_session(self, session_date: date, *, step: int) -> date | None:
        candidate = session_date + timedelta(days=step)
        for _ in range(MAX_SESSION_SEARCH_DAYS):
            if self.session_on(candidate).is_open:
                return candidate
            candidate += timedelta(days=step)
        return None

    def previous_open_session(self, session_date: date) -> date | None:
        return self._open_session(session_date, step=-1)

    def next_open_session(self, session_date: date) -> date | None:
        return self._open_session(session_date, step=1)


async def load_licensed_trading_calendar(
    config: LicensedCalendarFeedConfig,
    *,
    required_start: date,
    required_end: date,
    parser: CalendarManifestParser = parse_licensed_calendar_json,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    resolver: AddressResolver = resolve_host_addresses,
    transport: httpx.AsyncBaseTransport | None = None,
) -> LicensedTradingCalendar:
    """Fetch and fully validate a calendar before scheduler startup."""

    if type(required_start) is not date or type(required_end) is not date:
        raise TypeError("required calendar coverage must use dates")
    if required_end < required_start:
        raise ValueError("required calendar coverage is invalid")
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    try:
        content, media_type = await fetch_approved_https_bytes(
            config.endpoint,
            header_factory=config.header_factory,
            resolver=resolver,
            timeout_seconds=config.timeout_seconds,
            transport=transport,
        )
        manifest = parser(content, media_type, config)
        if not isinstance(manifest, LicensedCalendarManifest):
            raise ValueError("licensed_calendar_contract_invalid")
        if manifest.provider != config.provider or manifest.market is not config.market:
            raise ValueError("licensed_calendar_contract_invalid")
        if (
            manifest.generated_at > now + config.max_future_skew
            or now - manifest.generated_at > config.max_manifest_age
        ):
            raise ValueError("licensed_calendar_stale")
        if (
            manifest.coverage_start > required_start
            or manifest.coverage_end < required_end
        ):
            raise ValueError("licensed_calendar_coverage_incomplete")
    except (LicensedHttpError, TypeError, ValueError):
        raise CalendarUnavailable("licensed_calendar_unavailable") from None
    return LicensedTradingCalendar(manifest)


def calendar_required_range(
    reference_date: date,
    *,
    search_days: int = MAX_SESSION_SEARCH_DAYS,
) -> tuple[date, date]:
    """Return the startup coverage needed by previous/next-session decisions."""

    if type(reference_date) is not date:
        raise TypeError("reference_date must be a date")
    if type(search_days) is not int or not 1 <= search_days <= MAX_SESSION_SEARCH_DAYS:
        raise ValueError("search_days is invalid")
    return (
        reference_date - timedelta(days=search_days),
        reference_date + timedelta(days=search_days),
    )


__all__ = [
    "CalendarManifestParser",
    "LicensedCalendarFeedConfig",
    "LicensedCalendarManifest",
    "LicensedTradingCalendar",
    "calendar_required_range",
    "load_licensed_trading_calendar",
    "parse_licensed_calendar_json",
]
