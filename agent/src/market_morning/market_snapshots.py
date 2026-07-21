"""Licensed-provider-neutral market snapshot contracts and publication gate."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol


class MarketInstrument(StrEnum):
    NIKKEI_225 = "nikkei_225"
    SP_500 = "sp_500"
    NASDAQ_COMPOSITE = "nasdaq_composite"
    DJIA = "djia"
    USD_JPY = "usd_jpy"


class MarketDelayStatus(StrEnum):
    EOD = "eod"
    DELAYED = "delayed"
    REALTIME = "realtime"


class SnapshotGateStatus(StrEnum):
    READY = "ready"
    BLOCKED = "blocked"


class MarketDataUnavailable(RuntimeError):
    """Sanitized provider failure suitable for durable job classification."""


def _required(value: str, *, field_name: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field_name} must contain 1 to {maximum} characters")
    return normalized


def _aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _positive_decimal(value: Decimal, *, field_name: str) -> Decimal:
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field_name} must be a decimal") from error
    if not normalized.is_finite() or normalized <= 0:
        raise ValueError(f"{field_name} must be a positive finite decimal")
    return normalized


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    instrument: MarketInstrument | str
    provider: str
    session_date: date
    as_of: datetime
    value: Decimal
    previous_close: Decimal | None
    currency: str
    delay_status: MarketDelayStatus | str
    fetched_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument", MarketInstrument(self.instrument))
        object.__setattr__(
            self,
            "provider",
            _required(self.provider, field_name="provider", maximum=64),
        )
        if type(self.session_date) is not date:
            raise ValueError("session_date must be a date")
        as_of = _aware(self.as_of, field_name="as_of")
        fetched_at = _aware(self.fetched_at, field_name="fetched_at")
        if as_of > fetched_at + timedelta(minutes=5):
            raise ValueError("as_of cannot be materially later than fetched_at")
        object.__setattr__(self, "value", _positive_decimal(self.value, field_name="value"))
        if self.previous_close is not None:
            object.__setattr__(
                self,
                "previous_close",
                _positive_decimal(self.previous_close, field_name="previous_close"),
            )
        object.__setattr__(
            self,
            "currency",
            _required(self.currency, field_name="currency", maximum=8).upper(),
        )
        try:
            delay_status = MarketDelayStatus(self.delay_status)
        except ValueError as error:
            raise ValueError("delay_status is unsupported") from error
        object.__setattr__(self, "delay_status", delay_status)


class MarketDataAdapter(Protocol):
    provider: str
    instrument: MarketInstrument

    async def fetch(self, *, at: datetime) -> MarketSnapshot: ...


class FixtureMarketDataAdapter:
    """Deterministic adapter that cannot be confused with licensed production data."""

    def __init__(self, *, provider: str, snapshot: MarketSnapshot) -> None:
        canonical_provider = _required(provider, field_name="provider", maximum=64)
        if not canonical_provider.startswith("fixture_"):
            raise ValueError("fixture market-data providers must start with fixture_")
        if snapshot.provider != canonical_provider:
            raise ValueError("fixture adapter provider must match snapshot provider")
        self.provider = canonical_provider
        self.instrument = snapshot.instrument
        self._snapshot = snapshot

    async def fetch(self, *, at: datetime) -> MarketSnapshot:
        _aware(at, field_name="at")
        return self._snapshot


@dataclass(frozen=True, slots=True)
class SnapshotGateResult:
    status: SnapshotGateStatus
    snapshots_by_instrument: Mapping[MarketInstrument, MarketSnapshot]
    missing_instruments: tuple[MarketInstrument, ...]
    duplicate_instruments: tuple[MarketInstrument, ...]
    session_mismatches: tuple[MarketInstrument, ...]


def evaluate_snapshot_gate(
    snapshots: Sequence[MarketSnapshot],
    *,
    expected_sessions: Mapping[MarketInstrument | str, date],
) -> SnapshotGateResult:
    expected = {
        MarketInstrument(instrument): session_date
        for instrument, session_date in expected_sessions.items()
    }
    missing_expectations = tuple(
        instrument for instrument in MarketInstrument if instrument not in expected
    )
    if missing_expectations:
        raise ValueError("expected_sessions must include every required instrument")
    grouped: dict[MarketInstrument, list[MarketSnapshot]] = {
        instrument: [] for instrument in MarketInstrument
    }
    for snapshot in snapshots:
        grouped[snapshot.instrument].append(snapshot)
    missing = tuple(
        instrument for instrument in MarketInstrument if not grouped[instrument]
    )
    duplicates = tuple(
        instrument for instrument in MarketInstrument if len(grouped[instrument]) > 1
    )
    selected = {
        instrument: rows[0]
        for instrument, rows in grouped.items()
        if len(rows) == 1
    }
    mismatches = tuple(
        instrument
        for instrument, snapshot in selected.items()
        if snapshot.session_date != expected[instrument]
    )
    status = (
        SnapshotGateStatus.READY
        if not missing and not duplicates and not mismatches
        else SnapshotGateStatus.BLOCKED
    )
    return SnapshotGateResult(
        status=status,
        snapshots_by_instrument=selected,
        missing_instruments=missing,
        duplicate_instruments=duplicates,
        session_mismatches=mismatches,
    )


__all__ = [
    "FixtureMarketDataAdapter",
    "MarketDataAdapter",
    "MarketDataUnavailable",
    "MarketDelayStatus",
    "MarketInstrument",
    "MarketSnapshot",
    "SnapshotGateResult",
    "SnapshotGateStatus",
    "evaluate_snapshot_gate",
]
