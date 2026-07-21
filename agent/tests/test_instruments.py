from __future__ import annotations

import pytest

from backtest.index_provider_policy import provider_candidates
from backtest.instruments import (
    AmbiguousSymbolError,
    INDEX_SYMBOLS,
    detect_instrument_market,
    get_instrument,
    is_tradable,
    normalize_symbol,
    provider_symbol,
)


def test_fixed_index_order_and_metadata() -> None:
    assert INDEX_SYMBOLS == (
        "NIKKEI225.INDEX",
        "SP500.INDEX",
        "NASDAQCOMPOSITE.INDEX",
        "DJIA.INDEX",
    )
    assert get_instrument("SP500.INDEX").timezone == "America/New_York"
    assert detect_instrument_market("^N225") == "global_index"


@pytest.mark.parametrize(
    "alias,canonical",
    [
        (" ^N225 ", "NIKKEI225.INDEX"),
        ("日经225", "NIKKEI225.INDEX"),
        ("^GSPC", "SP500.INDEX"),
        ("S&P 500", "SP500.INDEX"),
        ("标普500", "SP500.INDEX"),
        ("nasdaq composite", "NASDAQCOMPOSITE.INDEX"),
        ("^IXIC", "NASDAQCOMPOSITE.INDEX"),
        ("DJIA", "DJIA.INDEX"),
        ("dow jones", "DJIA.INDEX"),
    ],
)
def test_explicit_aliases(alias: str, canonical: str) -> None:
    assert normalize_symbol(alias) == canonical
    assert normalize_symbol(normalize_symbol(alias)) == canonical


@pytest.mark.parametrize("alias", ["nasdaq", "纳指", "ナスダック"])
def test_ambiguous_nasdaq_aliases(alias: str) -> None:
    with pytest.raises(AmbiguousSymbolError) as exc:
        normalize_symbol(alias)
    assert exc.value.options == ("NASDAQCOMPOSITE.INDEX", "NASDAQ100.INDEX")


def test_unknown_and_a_share_are_preserved() -> None:
    assert normalize_symbol("000001.SZ") == "000001.SZ"
    assert detect_instrument_market("000001.SZ") is None
    assert is_tradable("000001.SZ") is True


def test_provider_mapping_and_policy_are_separate() -> None:
    assert provider_symbol("SP500.INDEX", "yahoo") == "^GSPC"
    assert provider_candidates("SP500.INDEX") == ("yahoo", "local")
    assert is_tradable("SP500.INDEX") is False
