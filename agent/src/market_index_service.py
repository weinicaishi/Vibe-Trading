"""Normalized daily snapshots for the fixed global-index catalog."""

from __future__ import annotations

import asyncio
import copy
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from backtest.index_provider_policy import (
    INDEX_PROVIDER_POLICY,
    INDEX_PROVIDER_POLICY_VERSION,
)
from backtest.instruments import INDEX_INSTRUMENTS, Instrument

logger = logging.getLogger(__name__)

ALLOWED_RANGES = ("1m", "3m", "6m", "1y")
_RANGE_OFFSETS = {
    "1m": pd.DateOffset(months=1),
    "3m": pd.DateOffset(months=3),
    "6m": pd.DateOffset(months=6),
    "1y": pd.DateOffset(years=1),
}


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    value: dict[str, Any]


def _default_loader_resolver(provider: str) -> Any:
    from backtest.loaders import registry

    registry._ensure_registered()
    loader_cls = registry.LOADER_REGISTRY.get(provider)
    if loader_cls is None:
        raise LookupError(f"Provider {provider!r} is not registered")
    return loader_cls()


class MarketIndexService:
    """Fetch, normalize, cache, and single-flight the four index snapshots."""

    def __init__(
        self,
        *,
        loader_resolver: Callable[[str], Any] = _default_loader_resolver,
        provider_policy: Mapping[str, tuple[str, ...]] = INDEX_PROVIDER_POLICY,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._loader_resolver = loader_resolver
        self._provider_policy = dict(provider_policy)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._cache: dict[tuple[Any, ...], _CacheEntry] = {}
        self._inflight: dict[tuple[Any, ...], asyncio.Task[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    def _cache_key(self, range_name: str) -> tuple[Any, ...]:
        policy = tuple(
            (instrument.canonical_symbol, self._provider_policy[instrument.canonical_symbol])
            for instrument in INDEX_INSTRUMENTS
        )
        return (range_name, "1D", INDEX_PROVIDER_POLICY_VERSION, policy)

    async def get_indices(self, range_name: str) -> dict[str, Any]:
        if range_name not in ALLOWED_RANGES:
            raise ValueError(f"INVALID_RANGE: expected one of {', '.join(ALLOWED_RANGES)}")

        key = self._cache_key(range_name)
        now_mono = self._monotonic()
        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached.expires_at > now_mono:
                return copy.deepcopy(cached.value)

            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(asyncio.to_thread(self._build_snapshot, range_name))
                self._inflight[key] = task

        try:
            result = await asyncio.shield(task)
        finally:
            async with self._lock:
                if self._inflight.get(key) is task and task.done():
                    self._inflight.pop(key, None)

        ttl = 60.0 if not result["errors"] else (15.0 if result["items"] else 5.0)
        async with self._lock:
            self._cache[key] = _CacheEntry(
                expires_at=self._monotonic() + ttl,
                value=copy.deepcopy(result),
            )
        return result

    def clear(self) -> None:
        """Clear snapshot state; intended for tests and controlled rollouts."""
        self._cache.clear()
        self._inflight.clear()

    def _window(self, instrument: Instrument, range_name: str) -> tuple[str, str]:
        local_date = self._now().astimezone(ZoneInfo(instrument.timezone)).date()
        end = pd.Timestamp(local_date)
        start = end - _RANGE_OFFSETS[range_name]
        return start.date().isoformat(), end.date().isoformat()

    def _build_snapshot(self, range_name: str) -> dict[str, Any]:
        windows = {
            instrument.canonical_symbol: self._window(instrument, range_name)
            for instrument in INDEX_INSTRUMENTS
        }
        pending = {instrument.canonical_symbol for instrument in INDEX_INSTRUMENTS}
        frames: dict[str, tuple[pd.DataFrame, str]] = {}
        last_provider: dict[str, str] = {}
        last_code: dict[str, str] = {}

        max_candidates = max(
            len(self._provider_policy[instrument.canonical_symbol])
            for instrument in INDEX_INSTRUMENTS
        )
        for candidate_index in range(max_candidates):
            groups: dict[tuple[str, str, str], list[str]] = {}
            for symbol in tuple(pending):
                candidates = self._provider_policy[symbol]
                if candidate_index >= len(candidates):
                    continue
                provider = candidates[candidate_index]
                start_date, end_date = windows[symbol]
                groups.setdefault((provider, start_date, end_date), []).append(symbol)

            for (provider, start_date, end_date), symbols in groups.items():
                for symbol in symbols:
                    last_provider[symbol] = provider
                try:
                    loader = self._loader_resolver(provider)
                    if not loader.is_available():
                        for symbol in symbols:
                            last_code[symbol] = "PROVIDER_UNAVAILABLE"
                        continue
                    fetched = loader.fetch(
                        symbols,
                        start_date,
                        end_date,
                        interval="1D",
                        fields=["trade_date", "open", "high", "low", "close"],
                    )
                except Exception as exc:  # provider details stay server-side
                    logger.warning(
                        "market-index fetch failed provider=%s endpoint=chart symbols=%s error=%s",
                        provider,
                        symbols,
                        type(exc).__name__,
                    )
                    for symbol in symbols:
                        last_code[symbol] = "PROVIDER_UNAVAILABLE"
                    continue

                for symbol in symbols:
                    frame = fetched.get(symbol) if isinstance(fetched, dict) else None
                    if isinstance(frame, pd.DataFrame) and not frame.empty:
                        frames[symbol] = (frame, provider)
                        pending.discard(symbol)
                    else:
                        last_code[symbol] = "NO_DATA"

        items: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for instrument in INDEX_INSTRUMENTS:
            symbol = instrument.canonical_symbol
            loaded = frames.get(symbol)
            if loaded is None:
                errors.append(
                    {
                        "symbol": symbol,
                        "provider": last_provider.get(symbol, "unknown"),
                        "code": last_code.get(symbol, "PROVIDER_UNAVAILABLE"),
                        "message": "Market data is temporarily unavailable.",
                    }
                )
                continue
            frame, provider = loaded
            try:
                items.append(
                    self._frame_to_item(
                        instrument,
                        frame,
                        provider,
                        *windows[symbol],
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "market-index schema error provider=%s symbol=%s error=%s",
                    provider,
                    symbol,
                    type(exc).__name__,
                )
                errors.append(
                    {
                        "symbol": symbol,
                        "provider": provider,
                        "code": "PROVIDER_SCHEMA_ERROR",
                        "message": "Market data is temporarily unavailable.",
                    }
                )

        as_of = self._now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return {
            "as_of": as_of,
            "range": range_name,
            "interval": "1D",
            "items": items,
            "errors": errors,
        }

    @staticmethod
    def _frame_to_item(
        instrument: Instrument,
        frame: pd.DataFrame,
        provider: str,
        window_start: str,
        window_end: str,
    ) -> dict[str, Any]:
        required = ("open", "high", "low", "close")
        missing = [column for column in required if column not in frame.columns]
        if missing:
            raise KeyError(f"missing columns: {missing}")

        normalized = frame.copy().sort_index().tail(260)
        series: list[dict[str, Any]] = []
        for index, row in normalized.iterrows():
            values = {column: float(row[column]) for column in required}
            if not all(math.isfinite(value) for value in values.values()):
                continue
            raw_date = row.get("trade_date", index)
            session_date = pd.Timestamp(raw_date).date().isoformat()
            series.append({"session_date": session_date, **values})
        if not series:
            raise ValueError("no finite OHLC rows")

        latest = series[-1]
        previous_close = series[-2]["close"] if len(series) >= 2 else None
        change = latest["close"] - previous_close if previous_close is not None else None
        change_percent = (
            change / previous_close
            if change is not None and previous_close not in (None, 0)
            else None
        )
        return {
            "symbol": instrument.canonical_symbol,
            "name": instrument.display_name,
            "name_ja": instrument.display_name_ja,
            "market": instrument.market,
            "region": instrument.region,
            "currency": instrument.currency,
            "timezone": instrument.timezone,
            "source": provider,
            "delay_status": "unknown",
            "tradable": instrument.tradable,
            "window_start": window_start,
            "window_end": window_end,
            "latest": latest,
            "previous_close": previous_close,
            "change": change,
            "change_percent": change_percent,
            "series": series,
        }


market_index_service = MarketIndexService()
