from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pandas as pd
import pytest

from backtest.instruments import INDEX_SYMBOLS
from src.market_index_service import MarketIndexService


def _frame(closes=(100.0, 102.0)) -> pd.DataFrame:
    dates = pd.date_range("2026-06-16", periods=len(closes), freq="D", name="trade_date")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [value + 1 for value in closes],
            "low": [value - 1 for value in closes],
            "close": closes,
            "volume": [0.0] * len(closes),
        },
        index=dates,
    )


class _Loader:
    name = "fake"

    def __init__(self, *, missing=(), delay=0.0, calls=None):
        self.missing = set(missing)
        self.delay = delay
        self.calls = calls if calls is not None else []

    def is_available(self):
        return True

    def fetch(self, codes, start_date, end_date, *, interval="1D", fields=None):
        self.calls.append((tuple(codes), start_date, end_date, interval))
        if self.delay:
            time.sleep(self.delay)
        return {code: _frame() for code in codes if code not in self.missing}


def _service(loader: _Loader, *, monotonic=lambda: 0.0) -> MarketIndexService:
    policy = {symbol: ("fake",) for symbol in INDEX_SYMBOLS}
    return MarketIndexService(
        loader_resolver=lambda provider: loader,
        provider_policy=policy,
        now=lambda: datetime(2026, 7, 17, 8, tzinfo=timezone.utc),
        monotonic=monotonic,
    )


@pytest.mark.asyncio
async def test_full_snapshot_contract_and_order() -> None:
    result = await _service(_Loader()).get_indices("1m")
    assert [item["symbol"] for item in result["items"]] == list(INDEX_SYMBOLS)
    assert result["errors"] == []
    assert result["items"][0]["latest"]["session_date"] == "2026-06-17"
    assert result["items"][0]["change"] == pytest.approx(2.0)
    assert result["items"][0]["change_percent"] == pytest.approx(0.02)
    assert "volume" not in result["items"][0]["series"][0]
    assert result["as_of"].endswith("Z")


@pytest.mark.asyncio
async def test_partial_failure_preserves_success_order() -> None:
    result = await _service(_Loader(missing={"SP500.INDEX"})).get_indices("1m")
    assert [item["symbol"] for item in result["items"]] == [
        "NIKKEI225.INDEX", "NASDAQCOMPOSITE.INDEX", "DJIA.INDEX",
    ]
    assert result["errors"][0]["symbol"] == "SP500.INDEX"
    assert result["errors"][0]["code"] == "NO_DATA"


@pytest.mark.asyncio
async def test_same_key_is_single_flight() -> None:
    calls: list = []
    service = _service(_Loader(delay=0.03, calls=calls))
    results = await asyncio.gather(*(service.get_indices("1m") for _ in range(10)))
    assert all(len(result["items"]) == 4 for result in results)
    # JP and US can have distinct local window_end dates, so at most two grouped calls.
    assert len(calls) <= 2


@pytest.mark.asyncio
async def test_success_cache_expires_at_sixty_seconds() -> None:
    clock = [0.0]
    calls: list = []
    service = _service(_Loader(calls=calls), monotonic=lambda: clock[0])
    await service.get_indices("1m")
    first_count = len(calls)
    clock[0] = 59.0
    await service.get_indices("1m")
    assert len(calls) == first_count
    clock[0] = 60.0
    await service.get_indices("1m")
    assert len(calls) > first_count


@pytest.mark.asyncio
async def test_invalid_range_rejected() -> None:
    with pytest.raises(ValueError, match="INVALID_RANGE"):
        await _service(_Loader()).get_indices("5m")
