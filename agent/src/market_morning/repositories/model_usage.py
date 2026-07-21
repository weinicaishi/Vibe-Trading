"""Idempotent persistence for provider-neutral model usage events."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.market_morning.model_usage import ModelUsageEvent
from src.market_morning.models import ModelUsageEventRecord


class ModelUsagePersistenceConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModelUsageWriteResult:
    usage_event_id: str
    usage_key: str


def build_model_usage_insert_statement(event: ModelUsageEvent):
    statement = mysql_insert(ModelUsageEventRecord.__table__).values(
        usage_event_id=event.usage_event_id,
        **event.to_record_values(),
    )
    return statement.on_duplicate_key_update(
        usage_event_id=ModelUsageEventRecord.__table__.c.usage_event_id
    )


def _lock_statement(usage_key: str):
    return (
        select(ModelUsageEventRecord)
        .where(ModelUsageEventRecord.usage_key == usage_key)
        .with_for_update()
    )


def _verify(record, *, event: ModelUsageEvent) -> None:
    if record is None:
        raise ModelUsagePersistenceConflict("model usage event could not be loaded")
    for field_name, expected in event.to_record_values().items():
        if getattr(record, field_name) != expected:
            raise ModelUsagePersistenceConflict(
                "usage key already belongs to different usage data"
            )


async def record_model_usage(
    session: AsyncSession,
    *,
    event: ModelUsageEvent,
) -> ModelUsageWriteResult:
    await session.execute(build_model_usage_insert_statement(event))
    record = (
        await session.execute(_lock_statement(event.usage_key))
    ).scalar_one()
    _verify(record, event=event)
    return ModelUsageWriteResult(
        usage_event_id=record.usage_event_id,
        usage_key=record.usage_key,
    )


__all__ = [
    "ModelUsagePersistenceConflict",
    "ModelUsageWriteResult",
    "build_model_usage_insert_statement",
    "record_model_usage",
]
