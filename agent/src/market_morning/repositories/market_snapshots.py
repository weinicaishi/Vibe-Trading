"""MySQL persistence for immutable Market Morning market snapshots."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.market_snapshots import MarketInstrument, MarketSnapshot
from src.market_morning.models import MarketSnapshotRecord, new_id


class MarketSnapshotConflict(RuntimeError):
    pass


class MarketSnapshotWriteStatus(StrEnum):
    INSERTED = "inserted"
    ALREADY_PRESENT = "already_present"


@dataclass(frozen=True, slots=True)
class MarketSnapshotWriteResult:
    status: MarketSnapshotWriteStatus
    snapshot_id: str


@dataclass(frozen=True, slots=True)
class PersistedMarketSnapshot:
    snapshot_id: str
    snapshot: MarketSnapshot


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise ValueError("snapshot_id must be a UUID") from error


def _mysql_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _payload(snapshot: MarketSnapshot) -> dict[str, str | None]:
    return {
        "instrument": snapshot.instrument.value,
        "provider": snapshot.provider,
        "session_date": snapshot.session_date.isoformat(),
        "as_of": snapshot.as_of.astimezone(timezone.utc).isoformat(),
        "value": str(snapshot.value),
        "previous_close": (
            None if snapshot.previous_close is None else str(snapshot.previous_close)
        ),
        "currency": snapshot.currency,
        "delay_status": snapshot.delay_status.value,
        "fetched_at": snapshot.fetched_at.astimezone(timezone.utc).isoformat(),
    }


def market_snapshot_sha256(snapshot: MarketSnapshot) -> str:
    encoded = json.dumps(
        _payload(snapshot),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_market_snapshot_insert_statement(
    *,
    snapshot_id: str,
    snapshot: MarketSnapshot,
    created_at: datetime,
):
    created = _mysql_utc(created_at, field_name="created_at")
    statement = mysql_insert(MarketSnapshotRecord.__table__).values(
        snapshot_id=_uuid(snapshot_id),
        instrument=snapshot.instrument.value,
        provider=snapshot.provider,
        session_date=snapshot.session_date,
        as_of=_mysql_utc(snapshot.as_of, field_name="snapshot.as_of"),
        value=snapshot.value,
        previous_close=snapshot.previous_close,
        currency=snapshot.currency,
        delay_status=snapshot.delay_status.value,
        fetched_at=_mysql_utc(snapshot.fetched_at, field_name="snapshot.fetched_at"),
        snapshot_sha256=market_snapshot_sha256(snapshot),
        created_at=created,
    )
    return statement.on_duplicate_key_update(
        snapshot_id=MarketSnapshotRecord.__table__.c.snapshot_id
    )


def _identity_statement(snapshot: MarketSnapshot):
    return select(MarketSnapshotRecord).where(
        MarketSnapshotRecord.instrument == snapshot.instrument.value,
        MarketSnapshotRecord.provider == snapshot.provider,
        MarketSnapshotRecord.session_date == snapshot.session_date,
        MarketSnapshotRecord.as_of
        == _mysql_utc(snapshot.as_of, field_name="snapshot.as_of"),
    )


async def persist_market_snapshot(
    session: AsyncSession,
    *,
    snapshot: MarketSnapshot,
    created_at: datetime,
) -> MarketSnapshotWriteResult:
    candidate_id = new_id()
    expected_hash = market_snapshot_sha256(snapshot)
    await session.execute(
        build_market_snapshot_insert_statement(
            snapshot_id=candidate_id,
            snapshot=snapshot,
            created_at=created_at,
        )
    )
    record = (await session.execute(_identity_statement(snapshot))).scalar_one_or_none()
    if record is None:
        raise MarketSnapshotConflict("market snapshot could not be reloaded")
    if record.snapshot_sha256 != expected_hash:
        raise MarketSnapshotConflict(
            "market snapshot identity already exists with a different payload"
        )
    return MarketSnapshotWriteResult(
        status=(
            MarketSnapshotWriteStatus.INSERTED
            if record.snapshot_id == candidate_id
            else MarketSnapshotWriteStatus.ALREADY_PRESENT
        ),
        snapshot_id=record.snapshot_id,
    )


async def load_expected_market_snapshots(
    session: AsyncSession,
    *,
    expected_sessions: dict[MarketInstrument, date],
    provider_by_instrument: dict[MarketInstrument, str],
) -> tuple[PersistedMarketSnapshot, ...]:
    if set(expected_sessions) != set(MarketInstrument):
        raise ValueError("expected_sessions must contain every instrument")
    if set(provider_by_instrument) != set(MarketInstrument):
        raise ValueError("provider_by_instrument must contain every instrument")
    conditions = [
        and_(
            MarketSnapshotRecord.instrument == instrument.value,
            MarketSnapshotRecord.provider == provider_by_instrument[instrument],
            MarketSnapshotRecord.session_date == expected_sessions[instrument],
        )
        for instrument in MarketInstrument
    ]
    records = (
        await session.execute(
            select(MarketSnapshotRecord)
            .where(or_(*conditions))
            .order_by(
                MarketSnapshotRecord.instrument,
                MarketSnapshotRecord.as_of.desc(),
                MarketSnapshotRecord.fetched_at.desc(),
                MarketSnapshotRecord.snapshot_id,
            )
        )
    ).scalars().all()
    latest: dict[MarketInstrument, PersistedMarketSnapshot] = {}
    for record in records:
        instrument = MarketInstrument(record.instrument)
        if instrument in latest:
            continue
        latest[instrument] = PersistedMarketSnapshot(
            snapshot_id=record.snapshot_id,
            snapshot=MarketSnapshot(
                instrument=instrument,
                provider=record.provider,
                session_date=record.session_date,
                as_of=record.as_of.replace(tzinfo=timezone.utc),
                value=record.value,
                previous_close=record.previous_close,
                currency=record.currency,
                delay_status=record.delay_status,
                fetched_at=record.fetched_at.replace(tzinfo=timezone.utc),
            ),
        )
    return tuple(
        latest[instrument]
        for instrument in MarketInstrument
        if instrument in latest
    )


__all__ = [
    "MarketSnapshotConflict",
    "MarketSnapshotWriteResult",
    "MarketSnapshotWriteStatus",
    "PersistedMarketSnapshot",
    "build_market_snapshot_insert_statement",
    "market_snapshot_sha256",
    "load_expected_market_snapshots",
    "persist_market_snapshot",
]
