"""Deterministic normalization for Japanese issuer search keys."""

from __future__ import annotations

import unicodedata

_LEGAL_PREFIXES = ("株式会社", "(株)")
_LEGAL_SUFFIXES = ("株式会社", "(株)")


def normalize_issuer_search_key(value: str) -> str:
    """Normalize an official name or approved alias for prefix search.

    NFKC handles full-width/half-width variants. Legal-form markers are removed
    only at the edges, and punctuation/whitespace are ignored. This function is
    the business rule; MySQL collation must not silently add more equivalences.
    """
    normalized = unicodedata.normalize("NFKC", value).strip()
    changed = True
    while normalized and changed:
        changed = False
        for prefix in _LEGAL_PREFIXES:
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix):].strip()
                changed = True
        for suffix in _LEGAL_SUFFIXES:
            if normalized.endswith(suffix):
                normalized = normalized[:-len(suffix)].strip()
                changed = True

    kept = []
    for char in normalized.casefold():
        category = unicodedata.category(char)
        if char.isspace() or category.startswith("P"):
            continue
        kept.append(char)
    return "".join(kept)
