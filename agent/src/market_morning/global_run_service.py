"""Application command and result contracts for global edition execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.market_morning.db import get_session_factory
from src.market_morning.global_runs import (
    GlobalEditionRunSpec,
    GlobalEditionSnapshotItem,
)
from src.market_morning.market_snapshots import MarketInstrument
from src.market_morning.market_snapshots import SnapshotGateStatus, evaluate_snapshot_gate
from src.market_morning.repositories.global_runs import (
    GlobalRunTerminalStatus,
    fail_global_edition_run,
    publish_global_edition_run,
    start_global_edition_run,
)
from src.market_morning.repositories.market_snapshots import (
    load_expected_market_snapshots,
)


@dataclass(frozen=True, slots=True)
class GlobalEditionRunCommand:
    spec: GlobalEditionRunSpec
    expected_market_sessions: Mapping[MarketInstrument, date]
    day_plan: dict[str, Any]

    def __post_init__(self) -> None:
        if set(self.expected_market_sessions) != set(MarketInstrument):
            raise ValueError("expected_market_sessions must contain all instruments")


@dataclass(frozen=True, slots=True)
class GlobalRunExecutionResult:
    run_id: str
    run_version: int
    publication_status: str
    publish_status: str
    email_permitted: bool


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def run_global_edition_generation(
    command: GlobalEditionRunCommand,
    *,
    provider_by_instrument: Mapping[MarketInstrument | str, str],
    session_factory: async_sessionmaker[AsyncSession] | Any | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> GlobalRunExecutionResult:
    providers = {
        MarketInstrument(instrument): provider.strip()
        for instrument, provider in provider_by_instrument.items()
    }
    if set(providers) != set(MarketInstrument) or any(not value for value in providers.values()):
        raise ValueError("provider_by_instrument must configure every instrument")
    factory = session_factory or get_session_factory()
    completed_at = clock()
    async with factory.begin() as session:
        started = await start_global_edition_run(session, spec=command.spec)
        persisted = await load_expected_market_snapshots(
            session,
            expected_sessions=dict(command.expected_market_sessions),
            provider_by_instrument=providers,
        )
        gate = evaluate_snapshot_gate(
            tuple(item.snapshot for item in persisted),
            expected_sessions=command.expected_market_sessions,
        )
        if gate.status is SnapshotGateStatus.BLOCKED:
            await fail_global_edition_run(
                session,
                run_id=started.run_id,
                spec=command.spec,
                reason_code="market_snapshot_gate_failed",
                completed_at=completed_at,
            )
            return GlobalRunExecutionResult(
                run_id=started.run_id,
                run_version=started.run_version,
                publication_status="failed",
                publish_status="blocked",
                email_permitted=False,
            )
        terminal = (
            GlobalRunTerminalStatus.LATE
            if command.spec.late
            else GlobalRunTerminalStatus.COMPLETE
        )
        published = await publish_global_edition_run(
            session,
            run_id=started.run_id,
            spec=command.spec,
            snapshot_items=tuple(
                GlobalEditionSnapshotItem(
                    item.snapshot_id,
                    item.snapshot.instrument,
                )
                for item in persisted
            ),
            publication_status=terminal,
            completed_at=completed_at,
        )
        return GlobalRunExecutionResult(
            run_id=published.run_id,
            run_version=published.run_version,
            publication_status=published.publication_status.value,
            publish_status=published.status.value,
            email_permitted=command.spec.email_permitted,
        )


__all__ = [
    "GlobalEditionRunCommand",
    "GlobalRunExecutionResult",
    "run_global_edition_generation",
]
