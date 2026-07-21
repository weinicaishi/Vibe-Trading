"""Canonical metadata and aliases for non-tradable market indices."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping


class AmbiguousSymbolError(ValueError):
    """Raised when a user alias can refer to multiple index families."""

    def __init__(self, value: str, options: tuple[str, ...]) -> None:
        self.value = value
        self.options = options
        super().__init__(
            f"Ambiguous symbol {value!r}; choose one of: {', '.join(options)}"
        )


@dataclass(frozen=True, slots=True)
class Instrument:
    canonical_symbol: str
    display_name: str
    display_name_ja: str
    asset_type: Literal["index"]
    market: Literal["global_index"]
    region: Literal["JP", "US"]
    currency: Literal["JPY", "USD"]
    timezone: str
    tradable: Literal[False]
    provider_symbols: Mapping[str, str]


def _instrument(
    symbol: str,
    name: str,
    name_ja: str,
    region: Literal["JP", "US"],
    currency: Literal["JPY", "USD"],
    timezone: str,
    yahoo_symbol: str,
) -> Instrument:
    return Instrument(
        canonical_symbol=symbol,
        display_name=name,
        display_name_ja=name_ja,
        asset_type="index",
        market="global_index",
        region=region,
        currency=currency,
        timezone=timezone,
        tradable=False,
        provider_symbols=MappingProxyType({"yahoo": yahoo_symbol}),
    )


INDEX_INSTRUMENTS: tuple[Instrument, ...] = (
    _instrument(
        "NIKKEI225.INDEX", "Nikkei 225", "日経平均株価", "JP", "JPY",
        "Asia/Tokyo", "^N225",
    ),
    _instrument(
        "SP500.INDEX", "S&P 500", "S&P 500種株価指数", "US", "USD",
        "America/New_York", "^GSPC",
    ),
    _instrument(
        "NASDAQCOMPOSITE.INDEX", "Nasdaq Composite", "ナスダック総合指数",
        "US", "USD", "America/New_York", "^IXIC",
    ),
    _instrument(
        "DJIA.INDEX", "Dow Jones Industrial Average", "ダウ・ジョーンズ工業株価平均",
        "US", "USD", "America/New_York", "^DJI",
    ),
)

INDEX_SYMBOLS: tuple[str, ...] = tuple(i.canonical_symbol for i in INDEX_INSTRUMENTS)
_BY_CANONICAL = {i.canonical_symbol: i for i in INDEX_INSTRUMENTS}


def _alias_key(value: str) -> str:
    return " ".join(value.strip().split()).casefold()


_ALIASES: dict[str, str] = {}
for canonical, aliases in {
    "NIKKEI225.INDEX": (
        "NIKKEI225.INDEX", "^N225", "N225", "nikkei 225", "nikkei225",
        "日経平均", "日経平均株価", "日经225",
    ),
    "SP500.INDEX": (
        "SP500.INDEX", "^GSPC", "GSPC", "s&p 500", "s&p500", "sp500",
        "S&P 500種株価指数", "标普500", "標普500",
    ),
    "NASDAQCOMPOSITE.INDEX": (
        "NASDAQCOMPOSITE.INDEX", "^IXIC", "IXIC", "nasdaq composite",
        "ナスダック総合", "ナスダック総合指数", "纳斯达克综合指数",
    ),
    "DJIA.INDEX": (
        "DJIA.INDEX", "^DJI", "DJI", "DJIA", "dow jones industrial average",
        "dow jones", "ダウ平均", "ダウ・ジョーンズ工業株価平均",
    ),
}.items():
    for alias in aliases:
        _ALIASES[_alias_key(alias)] = canonical

_AMBIGUOUS = {
    _alias_key(alias): ("NASDAQCOMPOSITE.INDEX", "NASDAQ100.INDEX")
    for alias in ("nasdaq", "纳指", "ナスダック")
}


def normalize_symbol(value: str) -> str:
    """Return a canonical index symbol, preserving unknown instruments."""
    cleaned = value.strip()
    key = _alias_key(cleaned)
    if key in _AMBIGUOUS:
        raise AmbiguousSymbolError(cleaned, _AMBIGUOUS[key])
    return _ALIASES.get(key, cleaned)


def get_instrument(value: str) -> Instrument | None:
    """Resolve an index instrument from a canonical symbol or explicit alias."""
    return _BY_CANONICAL.get(normalize_symbol(value))


def provider_symbol(canonical: str, provider: str) -> str:
    """Return a provider-specific ticker at the adapter boundary."""
    instrument = get_instrument(canonical)
    if instrument is None:
        raise KeyError(f"Unknown index instrument: {canonical}")
    try:
        return instrument.provider_symbols[provider]
    except KeyError as exc:
        raise KeyError(
            f"Provider {provider!r} does not support {instrument.canonical_symbol}"
        ) from exc


def detect_instrument_market(value: str) -> str | None:
    instrument = get_instrument(value)
    return instrument.market if instrument is not None else None


def is_tradable(value: str) -> bool:
    """Indices in this catalog are research-only; unknown symbols are unchanged."""
    instrument = get_instrument(value)
    return True if instrument is None else instrument.tradable
