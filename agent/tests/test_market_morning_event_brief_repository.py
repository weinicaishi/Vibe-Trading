"""MySQL EventBrief persistence and retry-state contracts."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)
EVENT_ID = "11111111-1111-4111-8111-111111111111"
BRIEF_ID = "22222222-2222-4222-8222-222222222222"
SOURCE_ID = "33333333-3333-4333-8333-333333333333"


class _Scalars:
    def __init__(self, rows=()):
        self.rows = tuple(rows)

    def all(self):
        return list(self.rows)


class _Result:
    def __init__(self, value=None, rows=()):
        self.value = value
        self.rows = tuple(rows)

    def scalar_one_or_none(self):
        return self.value

    def mappings(self):
        return self

    def all(self):
        return list(self.rows)

    def scalars(self):
        return _Scalars(self.rows)


class _Session:
    def __init__(self, *results):
        self.results = deque(results)
        self.statements = []
        self.added = []
        self.flush_count = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.popleft() if self.results else _Result()

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1


def _spec(**overrides):
    from src.market_morning.event_brief_generation import EventBriefGenerationSpec

    values = {
        "event_id": EVENT_ID,
        "event_version": 1,
        "generation_key": (
            f"event-brief:{EVENT_ID}:v1:fixture-model-v1:prompt-v1:s1"
        ),
        "model_version": "fixture-model-v1",
        "prompt_version": "prompt-v1",
        "started_at": NOW,
        "max_attempts": 3,
    }
    values.update(overrides)
    return EventBriefGenerationSpec(**values)


def _source():
    from src.market_morning.event_briefs import EventBriefSource

    return EventBriefSource(
        source_id=SOURCE_ID,
        provider="tdnet",
        original_url="https://example.com/source",
        reachable=True,
        evidence_text="2026年7月21日に業績予想を修正した。",
    )


def _published_brief():
    from src.market_morning.event_briefs import (
        parse_event_brief_v1,
        validate_event_brief,
    )

    draft = parse_event_brief_v1(
        {
            "schema_version": 1,
            "event_id": EVENT_ID,
            "event_version": 1,
            "title": "業績予想の修正",
            "occurred_at": NOW.isoformat(),
            "confirmed_facts": [
                {
                    "text": "2026年7月21日に業績予想を修正した。",
                    "source_ids": [SOURCE_ID],
                }
            ],
            "open_questions": ["影響は追加資料の確認が必要。"],
            "source_ids": [SOURCE_ID],
            "model_version": "fixture-model-v1",
            "review_status": "pending",
        }
    )
    return validate_event_brief(draft, sources=(_source(),)).brief


def _record(*, status: str = "running", attempt_count: int = 1):
    from src.market_morning.event_brief_generation import (
        encode_event_brief_generation_spec,
        event_brief_generation_spec_sha256,
    )

    spec = _spec()
    return SimpleNamespace(
        brief_id=BRIEF_ID,
        event_id=EVENT_ID,
        event_version=1,
        generation_key=spec.generation_key,
        schema_version=1,
        model_version=spec.model_version,
        prompt_version=spec.prompt_version,
        status=status,
        review_status="pending",
        attempt_count=attempt_count,
        max_attempts=3,
        spec=encode_event_brief_generation_spec(spec),
        spec_sha256=event_brief_generation_spec_sha256(spec),
        payload=None,
        payload_sha256=None,
        source_ids=[],
        validation_errors=[],
        primary_source_record_id=None,
        last_failure_code=None,
        last_failed_at=None,
        started_at=NOW.replace(tzinfo=None),
        completed_at=None,
        published_at=None,
        updated_at=NOW.replace(tzinfo=None),
    )


def test_event_brief_schema_requires_a_concrete_source_for_publishable_rows() -> None:
    from src.market_morning.models import (
        EventBriefRecord,
        EventBriefSourceLinkRecord,
    )

    brief_ddl = str(
        CreateTable(EventBriefRecord.__table__).compile(dialect=mysql.dialect())
    )
    source_ddl = str(
        CreateTable(EventBriefSourceLinkRecord.__table__).compile(
            dialect=mysql.dialect()
        )
    )

    assert "uq_mm_event_brief_generation_key" in brief_ddl
    assert "uq_mm_event_brief_generation_spec" in brief_ddl
    assert "ck_mm_event_brief_publishable_source" in brief_ddl
    assert "JSON_LENGTH(source_ids) > 0" in brief_ddl
    assert "primary_source_record_id IS NOT NULL" in brief_ddl
    assert "FOREIGN KEY(primary_source_record_id) REFERENCES mm_source_records" in brief_ddl
    assert "uq_mm_event_brief_source_link" in source_ddl
    assert "FOREIGN KEY(source_record_id) REFERENCES mm_source_records" in source_ddl
    assert "ENGINE=InnoDB" in brief_ddl and "CHARSET=utf8mb4" in brief_ddl


def test_event_brief_migration_follows_global_runs() -> None:
    migration = (
        REPO_ROOT
        / "agent/migrations/market_morning/versions/0010_market_morning_event_briefs.py"
    )

    assert migration.exists()
    text = migration.read_text(encoding="utf-8")
    assert 'down_revision: str | None = "0009_market_morning_global_runs"' in text
    assert '"mm_event_briefs"' in text
    assert '"mm_event_brief_sources"' in text
    assert "ck_mm_event_brief_publishable_source" in text


def test_event_brief_generation_spec_is_hash_stable_and_key_is_exact() -> None:
    from src.market_morning.event_brief_generation import (
        event_brief_generation_spec_sha256,
    )

    assert event_brief_generation_spec_sha256(_spec()) == (
        event_brief_generation_spec_sha256(_spec())
    )
    with pytest.raises(ValueError, match="generation_key"):
        _spec(generation_key="other")
    with pytest.raises(ValueError, match="max_attempts"):
        _spec(max_attempts=0)


def test_event_brief_insert_is_concurrency_safe_and_does_not_mutate_existing() -> None:
    from src.market_morning.repositories.event_briefs import (
        build_event_brief_insert_statement,
    )

    statement = build_event_brief_insert_statement(
        brief_id=BRIEF_ID,
        spec=_spec(),
    )
    sql = str(statement.compile(dialect=mysql.dialect()))
    update = sql.split("ON DUPLICATE KEY UPDATE", maxsplit=1)[1]

    assert "ON DUPLICATE KEY UPDATE" in sql
    assert "brief_id = mm_event_briefs.brief_id" in update
    assert "status" not in update
    assert "spec_sha256" not in update


def test_start_event_brief_generation_retries_failed_record_and_counts_attempt(
    monkeypatch,
) -> None:
    import asyncio

    import src.market_morning.repositories.event_briefs as repository

    record = _record(status="failed", attempt_count=1)
    record.last_failure_code = "prior_failure"
    monkeypatch.setattr(repository, "new_id", lambda: BRIEF_ID)
    result = asyncio.run(
        repository.start_event_brief_generation(
            _Session(_Result(), _Result(record)),
            spec=_spec(),
        )
    )

    assert result.brief_id == BRIEF_ID
    assert result.attempt_number == 2
    assert result.retry_permitted is True
    assert record.status == "running"
    assert record.attempt_count == 2
    assert record.last_failure_code == "prior_failure"


def test_start_event_brief_generation_returns_existing_terminal_payload_hash() -> None:
    import asyncio

    from src.market_morning.repositories.event_briefs import (
        start_event_brief_generation,
    )

    record = _record(status="published", attempt_count=1)
    record.payload_sha256 = "b" * 64
    result = asyncio.run(
        start_event_brief_generation(
            _Session(_Result(), _Result(record)),
            spec=_spec(),
        )
    )

    assert result.can_execute is False
    assert result.payload_sha256 == "b" * 64


def test_publish_event_brief_writes_immutable_payload_and_source_links(
    monkeypatch,
) -> None:
    import asyncio

    import src.market_morning.repositories.event_briefs as repository
    from src.market_morning.models import EventBriefSourceLinkRecord

    record = _record()
    monkeypatch.setattr(
        repository,
        "new_id",
        lambda: "44444444-4444-4444-8444-444444444444",
    )
    session = _Session(_Result(record), _Result(rows=()))
    result = asyncio.run(
        repository.publish_event_brief(
            session,
            brief_id=BRIEF_ID,
            spec=_spec(),
            brief=_published_brief(),
            completed_at=NOW,
        )
    )

    assert result.status.value == "published"
    assert record.status == "published"
    assert record.primary_source_record_id == SOURCE_ID
    assert record.source_ids == [SOURCE_ID]
    assert record.payload_sha256 is not None
    assert record.payload["sources"][0]["source_id"] == SOURCE_ID
    assert "evidence_text" not in repr(record.payload)
    assert len(session.added) == 1
    assert isinstance(session.added[0], EventBriefSourceLinkRecord)
    assert session.added[0].source_record_id == SOURCE_ID


def test_fail_event_brief_generation_retains_reason_and_terminal_attempt() -> None:
    import asyncio

    from src.market_morning.repositories.event_briefs import (
        fail_event_brief_generation,
    )

    retryable = _record(attempt_count=1)
    retry_result = asyncio.run(
        fail_event_brief_generation(
            _Session(_Result(retryable)),
            brief_id=BRIEF_ID,
            spec=_spec(),
            error_code="model_unavailable",
            failed_at=NOW,
        )
    )
    assert retry_result.retry_permitted is True
    assert retryable.status == "failed"
    assert retryable.last_failure_code == "model_unavailable"

    terminal = _record(attempt_count=3)
    terminal_result = asyncio.run(
        fail_event_brief_generation(
            _Session(_Result(terminal)),
            brief_id=BRIEF_ID,
            spec=_spec(),
            error_code="model_unavailable",
            failed_at=NOW,
        )
    )
    assert terminal_result.retry_permitted is False
    assert terminal.status == "failed"
