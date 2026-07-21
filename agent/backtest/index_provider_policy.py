"""Per-index provider order, kept separate from provider capabilities."""

from __future__ import annotations

from backtest.instruments import INDEX_SYMBOLS

INDEX_PROVIDER_POLICY_VERSION = 1

INDEX_PROVIDER_POLICY: dict[str, tuple[str, ...]] = {
    "NIKKEI225.INDEX": ("yahoo", "local"),
    "SP500.INDEX": ("yahoo", "local"),
    "NASDAQCOMPOSITE.INDEX": ("yahoo", "local"),
    "DJIA.INDEX": ("yahoo", "local"),
}


def provider_candidates(canonical_symbol: str) -> tuple[str, ...]:
    try:
        return INDEX_PROVIDER_POLICY[canonical_symbol]
    except KeyError as exc:
        raise KeyError(f"No provider policy for {canonical_symbol}") from exc


def validate_provider_policy() -> None:
    missing = set(INDEX_SYMBOLS) - set(INDEX_PROVIDER_POLICY)
    extra = set(INDEX_PROVIDER_POLICY) - set(INDEX_SYMBOLS)
    empty = [symbol for symbol, providers in INDEX_PROVIDER_POLICY.items() if not providers]
    if missing or extra or empty:
        raise ValueError(
            f"Invalid index provider policy: missing={sorted(missing)}, "
            f"extra={sorted(extra)}, empty={sorted(empty)}"
        )


validate_provider_policy()
