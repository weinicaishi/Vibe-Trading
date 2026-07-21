"""Licensed-provider-neutral HTTPS adapters for the five MVP observations."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from src.market_morning.http_reachability import AddressResolver, resolve_host_addresses
from src.market_morning.licensed_http import (
    ApprovedHttpsEndpoint,
    HeaderFactory,
    LicensedHttpError,
    fetch_approved_https_bytes,
)
from src.market_morning.market_snapshots import (
    MarketDataUnavailable,
    MarketDelayStatus,
    MarketInstrument,
    MarketSnapshot,
)

MarketObservationParser = Callable[
    [bytes, str, "LicensedMarketDataFeedConfig"],
    "LicensedMarketObservation",
]


def _required(value: Any, *, error_code: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(error_code)
    canonical = value.strip()
    if (
        not canonical
        or len(canonical) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in canonical)
    ):
        raise ValueError(error_code)
    return canonical


def _strict_date(value: Any) -> date:
    if not isinstance(value, str):
        raise ValueError("licensed_market_data_contract_invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError("licensed_market_data_contract_invalid") from None
    if parsed.isoformat() != value:
        raise ValueError("licensed_market_data_contract_invalid")
    return parsed


def _aware_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("licensed_market_data_contract_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("licensed_market_data_contract_invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("licensed_market_data_contract_invalid")
    return parsed


def _decimal_string(value: Any, *, nullable: bool = False) -> Decimal | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("licensed_market_data_contract_invalid")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise ValueError("licensed_market_data_contract_invalid") from None
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("licensed_market_data_contract_invalid")
    return parsed


@dataclass(frozen=True, slots=True)
class LicensedMarketDataFeedConfig:
    """One exact instrument/provider endpoint and its approved semantics."""

    provider: str
    instrument: MarketInstrument | str
    expected_currency: str
    endpoint_url: str = field(repr=False)
    allowed_base_urls: tuple[str, ...]
    allowed_delay_statuses: tuple[MarketDelayStatus | str, ...]
    header_factory: HeaderFactory | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    max_observation_age: timedelta = timedelta(days=7)
    max_future_skew: timedelta = timedelta(minutes=5)
    timeout_seconds: float = 10.0
    max_response_bytes: int = 256_000
    _endpoint: ApprovedHttpsEndpoint = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        provider = _required(
            self.provider,
            error_code="licensed_market_data_provider_invalid",
            maximum=64,
        )
        if provider.startswith("fixture_"):
            raise ValueError("licensed_market_data_provider_invalid")
        try:
            instrument = MarketInstrument(self.instrument)
        except ValueError:
            raise ValueError("licensed_market_data_instrument_invalid") from None
        currency = _required(
            self.expected_currency,
            error_code="licensed_market_data_currency_invalid",
            maximum=8,
        ).upper()
        if not currency.isascii() or not currency.isalpha():
            raise ValueError("licensed_market_data_currency_invalid")
        if (
            not isinstance(self.allowed_delay_statuses, tuple)
            or not 1 <= len(self.allowed_delay_statuses) <= len(MarketDelayStatus)
        ):
            raise ValueError("licensed_market_data_delays_invalid")
        try:
            delays = tuple(
                MarketDelayStatus(value) for value in self.allowed_delay_statuses
            )
        except ValueError:
            raise ValueError("licensed_market_data_delays_invalid") from None
        if len(set(delays)) != len(delays):
            raise ValueError("licensed_market_data_delays_invalid")
        if not timedelta(minutes=1) <= self.max_observation_age <= timedelta(days=14):
            raise ValueError("licensed_market_data_max_age_invalid")
        if not timedelta(0) <= self.max_future_skew <= timedelta(minutes=30):
            raise ValueError("licensed_market_data_future_skew_invalid")
        if not isinstance(self.timeout_seconds, (int, float)) or not (
            0.05 <= float(self.timeout_seconds) <= 30.0
        ):
            raise ValueError("licensed_market_data_timeout_invalid")
        endpoint = ApprovedHttpsEndpoint(
            url=self.endpoint_url,
            allowed_base_urls=self.allowed_base_urls,
            accepted_media_types=("application/json",),
            max_response_bytes=self.max_response_bytes,
        )
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "instrument", instrument)
        object.__setattr__(self, "expected_currency", currency)
        object.__setattr__(self, "allowed_delay_statuses", delays)
        object.__setattr__(self, "endpoint_url", endpoint.url)
        object.__setattr__(self, "allowed_base_urls", endpoint.allowed_base_urls)
        object.__setattr__(self, "_endpoint", endpoint)

    @property
    def endpoint(self) -> ApprovedHttpsEndpoint:
        return self._endpoint


@dataclass(frozen=True, slots=True)
class LicensedMarketObservation:
    provider: str
    instrument: MarketInstrument
    session_date: date
    as_of: datetime
    value: Decimal
    previous_close: Decimal | None
    currency: str
    delay_status: MarketDelayStatus

    def __post_init__(self) -> None:
        if type(self.session_date) is not date:
            raise ValueError("licensed_market_data_contract_invalid")
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("licensed_market_data_contract_invalid")


def parse_licensed_market_data_json(
    content: bytes,
    media_type: str,
    config: LicensedMarketDataFeedConfig,
) -> LicensedMarketObservation:
    """Parse one strict, normalized observation without accepting JSON floats."""

    if media_type != "application/json":
        raise ValueError("licensed_market_data_contract_invalid")
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("licensed_market_data_contract_invalid") from None
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "provider",
        "instrument",
        "session_date",
        "as_of",
        "value",
        "previous_close",
        "currency",
        "delay_status",
    }:
        raise ValueError("licensed_market_data_contract_invalid")
    if payload["schema_version"] != 1 or isinstance(payload["schema_version"], bool):
        raise ValueError("licensed_market_data_contract_invalid")
    provider = _required(
        payload["provider"],
        error_code="licensed_market_data_contract_invalid",
        maximum=64,
    )
    if provider != config.provider or payload["instrument"] != config.instrument.value:
        raise ValueError("licensed_market_data_contract_invalid")
    currency = _required(
        payload["currency"],
        error_code="licensed_market_data_contract_invalid",
        maximum=8,
    ).upper()
    if currency != config.expected_currency:
        raise ValueError("licensed_market_data_contract_invalid")
    try:
        delay_status = MarketDelayStatus(payload["delay_status"])
    except (TypeError, ValueError):
        raise ValueError("licensed_market_data_contract_invalid") from None
    if delay_status not in config.allowed_delay_statuses:
        raise ValueError("licensed_market_data_contract_invalid")
    value = _decimal_string(payload["value"])
    previous_close = _decimal_string(payload["previous_close"], nullable=True)
    assert isinstance(value, Decimal)
    return LicensedMarketObservation(
        provider=provider,
        instrument=config.instrument,
        session_date=_strict_date(payload["session_date"]),
        as_of=_aware_datetime(payload["as_of"]),
        value=value,
        previous_close=previous_close,
        currency=currency,
        delay_status=delay_status,
    )


class LicensedMarketDataAdapter:
    """Read one exact provider observation and emit the product snapshot type."""

    def __init__(
        self,
        config: LicensedMarketDataFeedConfig,
        *,
        parser: MarketObservationParser = parse_licensed_market_data_json,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        resolver: AddressResolver = resolve_host_addresses,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not callable(parser) or not callable(clock) or not callable(resolver):
            raise TypeError("licensed market-data dependencies must be callable")
        self.provider = config.provider
        self.instrument = config.instrument
        self._config = config
        self._parser = parser
        self._clock = clock
        self._resolver = resolver
        self._transport = transport

    async def fetch(self, *, at: datetime) -> MarketSnapshot:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("at must be timezone-aware")
        fetched_at = self._clock()
        if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        try:
            content, media_type = await fetch_approved_https_bytes(
                self._config.endpoint,
                header_factory=self._config.header_factory,
                resolver=self._resolver,
                timeout_seconds=self._config.timeout_seconds,
                transport=self._transport,
            )
            observation = self._parser(content, media_type, self._config)
            if (
                not isinstance(observation, LicensedMarketObservation)
                or observation.provider != self.provider
                or observation.instrument is not self.instrument
                or observation.currency != self._config.expected_currency
                or observation.delay_status
                not in self._config.allowed_delay_statuses
                or observation.as_of > fetched_at + self._config.max_future_skew
                or fetched_at - observation.as_of
                > self._config.max_observation_age
            ):
                raise ValueError("licensed_market_data_contract_invalid")
            return MarketSnapshot(
                instrument=observation.instrument,
                provider=observation.provider,
                session_date=observation.session_date,
                as_of=observation.as_of,
                value=observation.value,
                previous_close=observation.previous_close,
                currency=observation.currency,
                delay_status=observation.delay_status,
                fetched_at=fetched_at,
            )
        except (LicensedHttpError, TypeError, ValueError):
            raise MarketDataUnavailable("licensed_market_data_unavailable") from None


__all__ = [
    "LicensedMarketDataAdapter",
    "LicensedMarketDataFeedConfig",
    "LicensedMarketObservation",
    "MarketObservationParser",
    "parse_licensed_market_data_json",
]
