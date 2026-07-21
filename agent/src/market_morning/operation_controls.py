"""Database-backed application boundary for audited publication halts."""

from __future__ import annotations

from datetime import date, datetime, timezone

from src.market_morning.db import get_session_factory
from src.market_morning.repositories.manual_overrides import (
    OverrideMutationResult,
    create_publication_halt,
    revoke_publication_halt,
)


async def create_publication_halt_control(
    *, edition_date: date, reason_code: str, actor_reference: str
) -> OverrideMutationResult:
    factory = get_session_factory()
    async with factory.begin() as session:
        return await create_publication_halt(
            session,
            edition_date=edition_date,
            reason_code=reason_code,
            actor_reference=actor_reference,
            now=datetime.now(timezone.utc),
        )


async def revoke_publication_halt_control(
    *, edition_date: date, actor_reference: str
) -> OverrideMutationResult:
    factory = get_session_factory()
    async with factory.begin() as session:
        return await revoke_publication_halt(
            session,
            edition_date=edition_date,
            actor_reference=actor_reference,
            now=datetime.now(timezone.utc),
        )


__all__ = [
    "create_publication_halt_control",
    "revoke_publication_halt_control",
]
