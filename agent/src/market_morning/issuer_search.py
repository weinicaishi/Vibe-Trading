"""Issuer search ranking shared by fixtures and the MySQL-backed API."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import case, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.db import get_session_factory
from src.market_morning.issuer_master import (
    IssuerMasterValidationError,
    normalize_issuer_code,
)
from src.market_morning.models import Issuer, IssuerAlias
from src.market_morning.normalization import normalize_issuer_search_key


@dataclass(frozen=True, slots=True)
class IssuerCatalogEntry:
    issuer_id: str
    issuer_code: str
    legal_name_ja: str
    market_segment: str
    approved_aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IssuerSearchMatch:
    issuer_id: str
    issuer_code: str
    legal_name_ja: str
    market_segment: str
    match_kind: str
    matched_alias: str | None = None


def _normalized_query(query: str) -> tuple[str | None, str]:
    raw = query.strip()
    if not raw:
        return None, ""
    try:
        code = normalize_issuer_code(raw)
    except IssuerMasterValidationError:
        code = None
    return code, normalize_issuer_search_key(raw)


def search_catalog(
    entries: Iterable[IssuerCatalogEntry], query: str, *, limit: int = 10
) -> tuple[IssuerSearchMatch, ...]:
    """Search an in-memory catalog with the same deterministic product rules."""
    if not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    code_query, name_query = _normalized_query(query)
    if not code_query and not name_query:
        return ()

    ranked: list[tuple[int, str, IssuerSearchMatch]] = []
    for entry in entries:
        official_key = normalize_issuer_search_key(entry.legal_name_ja)
        aliases = tuple(
            (alias, normalize_issuer_search_key(alias)) for alias in entry.approved_aliases
        )
        priority: int | None = None
        kind = ""
        matched_alias: str | None = None
        if code_query == entry.issuer_code:
            priority, kind = 0, "code_exact"
        elif name_query == official_key:
            priority, kind = 1, "official_exact"
        else:
            exact_alias = next((alias for alias, key in aliases if key == name_query), None)
            prefix_alias = next((alias for alias, key in aliases if key.startswith(name_query)), None)
            if exact_alias is not None:
                priority, kind, matched_alias = 2, "alias_exact", exact_alias
            elif official_key.startswith(name_query):
                priority, kind = 3, "official_prefix"
            elif prefix_alias is not None:
                priority, kind, matched_alias = 4, "alias_prefix", prefix_alias
        if priority is not None:
            ranked.append(
                (
                    priority,
                    entry.issuer_code,
                    IssuerSearchMatch(
                        issuer_id=entry.issuer_id,
                        issuer_code=entry.issuer_code,
                        legal_name_ja=entry.legal_name_ja,
                        market_segment=entry.market_segment,
                        match_kind=kind,
                        matched_alias=matched_alias,
                    ),
                )
            )
    ranked.sort(key=lambda item: (item[0], item[1]))
    return tuple(item[2] for item in ranked[:limit])


def build_issuer_search_statement(query: str, *, limit: int = 10):
    """Build the bounded MySQL query used by the product API."""
    if not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    code_query, name_query = _normalized_query(query)
    if not code_query and not name_query:
        return None

    alias_display_match = (
        select(IssuerAlias.display_alias)
        .where(
            IssuerAlias.issuer_id == Issuer.issuer_id,
            IssuerAlias.review_status == "approved",
            IssuerAlias.effective_to.is_(None),
            IssuerAlias.normalized_alias.startswith(name_query),
        )
        .order_by(
            case((IssuerAlias.normalized_alias == name_query, 0), else_=1),
            IssuerAlias.normalized_alias,
            IssuerAlias.alias_id,
        )
        .limit(1)
        .correlate(Issuer)
        .scalar_subquery()
    )
    alias_key_match = (
        select(IssuerAlias.normalized_alias)
        .where(
            IssuerAlias.issuer_id == Issuer.issuer_id,
            IssuerAlias.review_status == "approved",
            IssuerAlias.effective_to.is_(None),
            IssuerAlias.normalized_alias.startswith(name_query),
        )
        .order_by(
            case((IssuerAlias.normalized_alias == name_query, 0), else_=1),
            IssuerAlias.normalized_alias,
            IssuerAlias.alias_id,
        )
        .limit(1)
        .correlate(Issuer)
        .scalar_subquery()
    )
    has_alias = exists(
        select(IssuerAlias.alias_id).where(
            IssuerAlias.issuer_id == Issuer.issuer_id,
            IssuerAlias.review_status == "approved",
            IssuerAlias.effective_to.is_(None),
            IssuerAlias.normalized_alias.startswith(name_query),
        )
    )
    official_prefix = Issuer.normalized_search_key.startswith(name_query)
    predicates = [official_prefix, has_alias]
    if code_query:
        predicates.insert(0, Issuer.issuer_code == code_query)

    rank_rules = []
    if code_query:
        rank_rules.append((Issuer.issuer_code == code_query, 0))
    rank_rules.extend(
        (
            (Issuer.normalized_search_key == name_query, 1),
            (alias_key_match == name_query, 2),
            (official_prefix, 3),
            (has_alias, 4),
        )
    )
    rank = case(*rank_rules, else_=9)
    return (
        select(
            Issuer.issuer_id,
            Issuer.issuer_code,
            Issuer.legal_name_ja,
            Issuer.market_segment,
            alias_display_match.label("matched_alias"),
        )
        .where(Issuer.active_status == "active", Issuer.effective_to.is_(None), or_(*predicates))
        .order_by(rank, Issuer.issuer_code)
        .limit(limit)
    )


async def search_issuers(
    session: AsyncSession, query: str, *, limit: int = 10
) -> tuple[IssuerSearchMatch, ...]:
    statement = build_issuer_search_statement(query, limit=limit)
    if statement is None:
        return ()
    rows = (await session.execute(statement)).mappings().all()
    matches: list[IssuerSearchMatch] = []
    code_query, name_query = _normalized_query(query)
    for row in rows:
        official_key = normalize_issuer_search_key(row["legal_name_ja"])
        alias = row["matched_alias"]
        alias_key = normalize_issuer_search_key(alias or "")
        if code_query == row["issuer_code"]:
            kind = "code_exact"
        elif name_query == official_key:
            kind = "official_exact"
        elif alias and name_query == alias_key:
            kind = "alias_exact"
        elif official_key.startswith(name_query):
            kind = "official_prefix"
        else:
            kind = "alias_prefix"
        matches.append(
            IssuerSearchMatch(
                issuer_id=row["issuer_id"],
                issuer_code=row["issuer_code"],
                legal_name_ja=row["legal_name_ja"],
                market_segment=row["market_segment"],
                match_kind=kind,
                matched_alias=alias,
            )
        )
    return tuple(matches)


async def search_issuer_catalog(query: str, *, limit: int = 10) -> tuple[IssuerSearchMatch, ...]:
    """Open a read session only after the route has checked the feature flag."""
    factory = get_session_factory()
    async with factory() as session:
        return await search_issuers(session, query, limit=limit)


__all__ = [
    "IssuerCatalogEntry",
    "IssuerSearchMatch",
    "build_issuer_search_statement",
    "search_catalog",
    "search_issuer_catalog",
    "search_issuers",
]
