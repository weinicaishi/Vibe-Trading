from __future__ import annotations

import pandas as pd

from backtest import benchmark


def _frame():
    return pd.DataFrame(
        {"close": [100.0, 101.0]},
        index=pd.date_range("2026-01-01", periods=2, name="trade_date"),
    )


def test_explicit_index_uses_canonical_policy_loader(monkeypatch) -> None:
    seen = []

    class Loader:
        def is_available(self):
            return True

        def fetch(self, codes, start_date, end_date, *, interval="1D", fields=None):
            seen.extend(codes)
            return {codes[0]: _frame()}

    from backtest.loaders import registry

    monkeypatch.setattr(registry, "_ensure_registered", lambda: None)
    monkeypatch.setattr(registry, "LOADER_REGISTRY", {"yahoo": Loader})
    result = benchmark.resolve_benchmark(
        ["AAPL.US"], "yahoo", "2026-01-01", "2026-01-02", explicit="^GSPC"
    )
    assert result is not None
    assert result.ticker == "SP500.INDEX"
    assert seen == ["SP500.INDEX"]


def test_spy_remains_a_legitimate_explicit_tradable_benchmark(monkeypatch) -> None:
    monkeypatch.setattr(benchmark, "_fetch_benchmark", lambda *args: _frame())
    result = benchmark.resolve_benchmark(
        ["AAPL.US"], "yfinance", "2026-01-01", "2026-01-02", explicit="SPY"
    )
    assert result is not None
    assert result.ticker == "SPY"
