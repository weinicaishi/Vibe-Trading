"""Network-free verification for the global-index vertical slice.

This script intentionally uses only runtime dependencies so production images
that omit pytest can still validate the feature after a source-only mount.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd

from backtest.engines._market_hooks import _detect_market
from backtest.index_provider_policy import provider_candidates
from backtest.instruments import INDEX_SYMBOLS, normalize_symbol, provider_symbol
from backtest.loaders.registry import FALLBACK_CHAINS
from backtest.loaders.yahoo_loader import DataLoader as YahooLoader
from src.market_data import detect_source, fetch_market_data
from src.market_index_service import MarketIndexService


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.0, 102.0],
            "volume": [0.0, 0.0],
        },
        index=pd.date_range("2026-07-15", periods=2, name="trade_date"),
    )


class _Loader:
    def __init__(self) -> None:
        self.calls = 0

    def is_available(self) -> bool:
        return True

    def fetch(self, codes, start_date, end_date, *, interval="1D", fields=None):
        self.calls += 1
        return {code: _frame() for code in codes}


async def _verify_service() -> dict:
    loader = _Loader()
    service = MarketIndexService(
        loader_resolver=lambda _provider: loader,
        provider_policy={symbol: ("fake",) for symbol in INDEX_SYMBOLS},
        now=lambda: datetime(2026, 7, 17, 8, tzinfo=timezone.utc),
        monotonic=lambda: 0.0,
    )
    first, second = await asyncio.gather(
        service.get_indices("1m"), service.get_indices("1m")
    )
    assert [item["symbol"] for item in first["items"]] == list(INDEX_SYMBOLS)
    assert first == second
    assert loader.calls <= 2
    assert first["items"][0]["change"] == 2.0
    assert first["items"][0]["change_percent"] == 0.02
    assert "volume" not in first["items"][0]["series"][0]
    return first


def main() -> None:
    assert normalize_symbol("^N225") == "NIKKEI225.INDEX"
    assert normalize_symbol("标普500") == "SP500.INDEX"
    assert normalize_symbol("^IXIC") == "NASDAQCOMPOSITE.INDEX"
    assert normalize_symbol("DJIA") == "DJIA.INDEX"
    assert provider_symbol("SP500.INDEX", "yahoo") == "^GSPC"
    assert provider_candidates("SP500.INDEX") == ("yahoo", "local")

    # A-share routing and its ordered fallback are an explicit non-regression boundary.
    assert detect_source("000001.SZ") == "tencent"
    assert _detect_market("000001.SZ") == "a_share"
    assert FALLBACK_CHAINS["a_share"] == [
        "tencent", "mootdx", "eastmoney", "baostock", "akshare", "tushare", "local",
    ]
    assert detect_source("^GSPC") == "yahoo"
    assert _detect_market("SP500.INDEX") == "global_index"
    normalized_fetch = fetch_market_data(
        codes=["^GSPC"],
        start_date="2026-07-01",
        end_date="2026-07-17",
        source="auto",
        loader_resolver=lambda _provider: _Loader,
    )
    assert "SP500.INDEX" in normalized_fetch and "^GSPC" not in normalized_fetch

    rows = [{
        "trade_date": int(pd.Timestamp("2026-07-16", tz="UTC").timestamp()),
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 0.0,
    }]
    with patch("backtest.loaders.yahoo_loader.yahoo_client.get_chart", return_value=rows) as chart:
        result = YahooLoader().fetch(["SP500.INDEX"], "2026-07-01", "2026-07-17")
    assert "SP500.INDEX" in result
    assert chart.call_args.args[0] == "^GSPC"

    snapshot = asyncio.run(_verify_service())

    import api_server
    from fastapi.testclient import TestClient
    from src.api import market_routes

    paths = {path for route in api_server.app.routes if (path := getattr(route, "path", None))}
    assert "/market/indices" in paths

    class _Service:
        async def get_indices(self, range_name: str):
            return {**snapshot, "range": range_name}

    market_routes.market_index_service = _Service()
    os.environ["VIBE_MARKET_INDICES_ENABLED"] = "true"
    market_routes._market_rate_limiter.reset()
    client = TestClient(api_server.app, client=("127.0.0.1", 50000))
    response = client.get("/market/indices", params={"range": "1m"})
    assert response.status_code == 200, response.text
    assert [item["symbol"] for item in response.json()["items"]] == list(INDEX_SYMBOLS)
    assert client.get("/market/indices", params={"range": "5m"}).status_code == 422
    print("market-index verification: ok")

    if "--network" in sys.argv:
        live = asyncio.run(MarketIndexService().get_indices("1m"))
        print(json.dumps({
            "items": [
                {
                    "symbol": item["symbol"],
                    "source": item["source"],
                    "bars": len(item["series"]),
                    "session_date": item["latest"]["session_date"],
                }
                for item in live["items"]
            ],
            "errors": [
                {"symbol": error["symbol"], "provider": error["provider"], "code": error["code"]}
                for error in live["errors"]
            ],
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
