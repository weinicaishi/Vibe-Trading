from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import mysql


NOW = datetime(2026, 7, 21, 1, 0, tzinfo=timezone.utc)
BRIEF_ID = "11111111-1111-4111-8111-111111111111"


class _Result:
    def __init__(self, *, scalar=None):
        self.scalar = scalar

    def scalar_one(self):
        return self.scalar


class _Session:
    def __init__(self, *results):
        self.results = deque(results)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        if self.results:
            return self.results.popleft()
        return _Result()


def _mysql_sql(statement) -> str:
    return str(
        statement.compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_model_usage_schema_is_hash_only_and_migrated_after_beta_privacy() -> None:
    from src.market_morning.models import ModelUsageEventRecord
    from src.market_morning.runtime import EXPECTED_MARKET_MORNING_SCHEMA_REVISION

    columns = set(ModelUsageEventRecord.__table__.columns.keys())
    assert {
        "usage_event_id",
        "usage_key",
        "brief_id",
        "attempt_number",
        "provider",
        "model",
        "usage_status",
        "cost_status",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "billable_cost_micros",
        "currency",
        "provider_request_id_sha256",
        "occurred_at",
        "created_at",
    } <= columns
    assert "provider_request_id" not in columns
    assert any(
        constraint.name == "uq_mm_model_usage_key"
        for constraint in ModelUsageEventRecord.__table__.constraints
    )
    assert any(
        constraint.name == "ck_mm_model_usage_non_negative"
        for constraint in ModelUsageEventRecord.__table__.constraints
    )

    migration = Path(
        "agent/migrations/market_morning/versions/0016_market_morning_model_usage.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision: str | None = "0015_market_morning_beta_privacy"' in migration
    assert 'revision: str = "0016_market_morning_model_usage"' in migration
    assert '"mm_model_usage_events"' in migration
    assert 'ondelete="CASCADE"' in migration
    assert 'name="ck_mm_model_usage_non_negative"' in migration
    assert EXPECTED_MARKET_MORNING_SCHEMA_REVISION == "0017_market_morning_content_reports"


def test_model_usage_report_validates_tokens_cost_currency_and_request_identity() -> None:
    from src.market_morning.model_usage import ModelUsageReport

    reported = ModelUsageReport(
        provider="openai",
        model="gpt-example-2026-07",
        input_tokens=120,
        output_tokens=30,
        billable_cost_micros=245,
        currency="usd",
        provider_request_id="request-secret-123",
    )
    assert reported.usage_status == "reported"
    assert reported.cost_status == "reported"
    assert reported.total_tokens == 150
    assert reported.currency == "USD"

    missing = ModelUsageReport.missing(provider="openai", model="gpt-example")
    assert missing.usage_status == "missing"
    assert missing.cost_status == "unpriced"
    assert missing.total_tokens is None

    with pytest.raises(ValueError, match="both be provided"):
        ModelUsageReport(provider="openai", model="gpt", input_tokens=1)
    with pytest.raises(ValueError, match="currency"):
        ModelUsageReport(
            provider="openai",
            model="gpt",
            input_tokens=1,
            output_tokens=1,
            billable_cost_micros=1,
        )
    with pytest.raises(ValueError, match="non-negative"):
        ModelUsageReport(
            provider="openai",
            model="gpt",
            input_tokens=-1,
            output_tokens=1,
        )
    with pytest.raises(ValueError, match="non-negative integer"):
        ModelUsageReport(
            provider="openai",
            model="gpt",
            input_tokens=1.5,
            output_tokens=1,
        )


def test_usage_event_hashes_provider_request_id_and_never_exposes_raw_value() -> None:
    from src.market_morning.model_usage import ModelUsageReport, build_model_usage_event

    raw_request_id = "request-secret-123"
    event = build_model_usage_event(
        brief_id=BRIEF_ID,
        attempt_number=2,
        report=ModelUsageReport(
            provider="openai",
            model="gpt-example",
            input_tokens=120,
            output_tokens=30,
            provider_request_id=raw_request_id,
        ),
        occurred_at=NOW,
    )

    values = event.to_record_values()
    assert values["usage_key"] == f"event-brief:{BRIEF_ID}:attempt:2"
    assert values["provider_request_id_sha256"] is not None
    assert len(values["provider_request_id_sha256"]) == 64
    assert raw_request_id not in repr(event)
    assert raw_request_id not in repr(values)
    assert "provider_request_id" not in values


def test_usage_insert_is_mysql_idempotent_and_repository_rejects_identity_drift() -> None:
    from src.market_morning.model_usage import ModelUsageReport, build_model_usage_event
    from src.market_morning.repositories.model_usage import (
        ModelUsagePersistenceConflict,
        build_model_usage_insert_statement,
        record_model_usage,
    )

    event = build_model_usage_event(
        brief_id=BRIEF_ID,
        attempt_number=1,
        report=ModelUsageReport(
            provider="openai",
            model="gpt-example",
            input_tokens=10,
            output_tokens=5,
        ),
        occurred_at=NOW,
        usage_event_id="22222222-2222-4222-8222-222222222222",
    )
    sql = _mysql_sql(build_model_usage_insert_statement(event))
    assert "INSERT INTO mm_model_usage_events" in sql
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert "provider_request_id_sha256" in sql
    assert "request-secret" not in sql

    matching = SimpleNamespace(**event.to_record_values())
    matching.usage_event_id = event.usage_event_id
    session = _Session(_Result(), _Result(scalar=matching))
    result = asyncio.run(record_model_usage(session, event=event))
    assert result.usage_event_id == event.usage_event_id

    drifted = SimpleNamespace(**event.to_record_values())
    drifted.usage_event_id = event.usage_event_id
    drifted.input_tokens = 99
    session = _Session(_Result(), _Result(scalar=drifted))
    with pytest.raises(ModelUsagePersistenceConflict, match="different usage"):
        asyncio.run(record_model_usage(session, event=event))
