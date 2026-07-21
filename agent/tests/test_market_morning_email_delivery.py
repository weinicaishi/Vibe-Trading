"""Durable, privacy-safe Market Morning email delivery storage."""

from __future__ import annotations

from pathlib import Path
from collections import deque
from datetime import date, datetime, timezone
from types import SimpleNamespace

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)
USER_ID = "11111111-1111-4111-8111-111111111111"
RUN_ID = "22222222-2222-4222-8222-222222222222"
ATTEMPT_ID = "33333333-3333-4333-8333-333333333333"
EVENT_ROW_ID = "44444444-4444-4444-8444-444444444444"


class _Result:
    def __init__(self, value=None):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Session:
    def __init__(self, *results):
        self.results = deque(results)
        self.statements = []
        self.flush_count = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.popleft() if self.results else _Result()

    async def flush(self):
        self.flush_count += 1


def _record(*, status="pending", attempt_count=0):
    return SimpleNamespace(
        delivery_attempt_id=ATTEMPT_ID,
        user_id=USER_ID,
        global_run_id=RUN_ID,
        edition_date=date(2026, 7, 21),
        channel="email",
        idempotency_key=(
            f"email:2026-07-21:{USER_ID}:global-run:{RUN_ID}"
        ),
        status=status,
        provider_message_id=None,
        deep_link_token_sha256="a" * 64,
        attempt_count=attempt_count,
        max_attempts=3,
        last_error_code=None,
        requested_at=NOW.replace(tzinfo=None),
        sent_at=None,
        delivered_at=None,
        clicked_at=None,
        failed_at=None,
        updated_at=NOW.replace(tzinfo=None),
    )


def _provider_event_record(*, outcome="pending"):
    return SimpleNamespace(
        provider_event_row_id=EVENT_ROW_ID,
        delivery_attempt_id=None,
        provider="resend",
        provider_event_id="event-1",
        provider_message_id="provider-message-1",
        event_type="delivered",
        payload_sha256="b" * 64,
        outcome=outcome,
        error_code=None,
        received_at=NOW.replace(tzinfo=None),
        processed_at=None,
    )


def test_delivery_attempt_schema_is_idempotent_and_stores_no_raw_destination() -> None:
    from src.market_morning.models import DeliveryAttemptRecord

    ddl = str(
        CreateTable(DeliveryAttemptRecord.__table__).compile(
            dialect=mysql.dialect()
        )
    )

    assert "uq_mm_delivery_user_date_channel" in ddl
    assert "uq_mm_delivery_idempotency_key" in ddl
    assert "uq_mm_delivery_provider_message" in ddl
    assert "ck_mm_delivery_status" in ddl
    assert "FOREIGN KEY(user_id) REFERENCES mm_users" in ddl
    assert "FOREIGN KEY(global_run_id) REFERENCES mm_global_edition_runs" in ddl
    assert "deep_link_token_sha256" in ddl
    assert "email_address" not in ddl
    assert "deep_link_token VARCHAR" not in ddl
    assert "ENGINE=InnoDB" in ddl and "CHARSET=utf8mb4" in ddl


def test_delivery_attempt_migration_follows_event_briefs() -> None:
    migration = (
        REPO_ROOT
        / "agent/migrations/market_morning/versions/0011_market_morning_delivery_attempts.py"
    )

    assert migration.exists()
    text = migration.read_text(encoding="utf-8")
    assert 'down_revision: str | None = "0010_market_morning_event_briefs"' in text
    assert '"mm_delivery_attempts"' in text
    assert "deep_link_token_sha256" in text
    assert "email_address" not in text


def test_delivery_provider_event_schema_is_idempotent_and_hash_only() -> None:
    from src.market_morning.models import DeliveryProviderEventRecord

    ddl = str(
        CreateTable(DeliveryProviderEventRecord.__table__).compile(
            dialect=mysql.dialect()
        )
    )

    assert "uq_mm_delivery_provider_event" in ddl
    assert "ck_mm_delivery_provider_event_type" in ddl
    assert "ck_mm_delivery_provider_event_outcome" in ddl
    assert "payload_sha256" in ddl
    assert "raw_payload" not in ddl
    assert "signature" not in ddl
    assert "FOREIGN KEY(delivery_attempt_id) REFERENCES mm_delivery_attempts" in ddl


def test_delivery_provider_event_migration_follows_attempts() -> None:
    migration = (
        REPO_ROOT
        / "agent/migrations/market_morning/versions/0012_market_morning_delivery_webhooks.py"
    )

    assert migration.exists()
    text = migration.read_text(encoding="utf-8")
    assert 'down_revision: str | None = "0011_market_morning_delivery_attempts"' in text
    assert '"mm_delivery_provider_events"' in text
    assert "payload_sha256" in text
    assert "raw_payload" not in text


def test_delivery_attempt_insert_is_concurrency_safe_and_exactly_keyed() -> None:
    from src.market_morning.repositories.email_delivery import (
        build_delivery_attempt_insert_statement,
        delivery_idempotency_key,
    )

    key = delivery_idempotency_key(
        user_id=USER_ID,
        edition_date=date(2026, 7, 21),
        global_run_id=RUN_ID,
    )
    statement = build_delivery_attempt_insert_statement(
        delivery_attempt_id=ATTEMPT_ID,
        user_id=USER_ID,
        global_run_id=RUN_ID,
        edition_date=date(2026, 7, 21),
        idempotency_key=key,
        deep_link_token_sha256="a" * 64,
        max_attempts=3,
        requested_at=NOW,
    )
    sql = str(statement.compile(dialect=mysql.dialect()))
    update = sql.split("ON DUPLICATE KEY UPDATE", maxsplit=1)[1]

    assert key == f"email:2026-07-21:{USER_ID}:global-run:{RUN_ID}"
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert "delivery_attempt_id = mm_delivery_attempts.delivery_attempt_id" in update
    assert "deep_link_token_sha256" not in update
    assert "status" not in update


def test_create_delivery_attempt_returns_existing_matching_row(monkeypatch) -> None:
    import asyncio

    import src.market_morning.repositories.email_delivery as repository

    record = _record()
    monkeypatch.setattr(repository, "new_id", lambda: ATTEMPT_ID)
    result = asyncio.run(
        repository.create_delivery_attempt(
            _Session(_Result(), _Result(record)),
            user_id=USER_ID,
            global_run_id=RUN_ID,
            edition_date=date(2026, 7, 21),
            deep_link_token_sha256="a" * 64,
            max_attempts=3,
            requested_at=NOW,
        )
    )

    assert result.delivery_attempt_id == ATTEMPT_ID
    assert result.status.value == "created"


def test_delivery_attempt_state_machine_retries_and_preserves_provider_identity() -> None:
    import asyncio

    from src.market_morning.repositories.email_delivery import (
        complete_delivery_attempt,
        fail_delivery_attempt,
        start_delivery_attempt,
    )

    record = _record()
    started = asyncio.run(
        start_delivery_attempt(
            _Session(_Result(record)),
            delivery_attempt_id=ATTEMPT_ID,
            started_at=NOW,
        )
    )
    assert started.can_execute is True
    assert record.status == "sending"
    assert record.attempt_count == 1

    failed = asyncio.run(
        fail_delivery_attempt(
            _Session(_Result(record)),
            delivery_attempt_id=ATTEMPT_ID,
            error_code="provider_unavailable",
            failed_at=NOW,
        )
    )
    assert failed.retry_permitted is True
    assert record.status == "failed"

    asyncio.run(
        start_delivery_attempt(
            _Session(_Result(record)),
            delivery_attempt_id=ATTEMPT_ID,
            started_at=NOW,
        )
    )
    completed = asyncio.run(
        complete_delivery_attempt(
            _Session(_Result(record)),
            delivery_attempt_id=ATTEMPT_ID,
            provider_message_id="provider-message-1",
            sent_at=NOW,
        )
    )

    assert completed.status.value == "sent"
    assert record.attempt_count == 2
    assert record.provider_message_id == "provider-message-1"


def test_suppress_delivery_attempt_is_terminal_and_retains_reason() -> None:
    import asyncio

    from src.market_morning.repositories.email_delivery import (
        start_delivery_attempt,
        suppress_delivery_attempt,
    )

    record = _record()
    result = asyncio.run(
        suppress_delivery_attempt(
            _Session(_Result(record)),
            delivery_attempt_id=ATTEMPT_ID,
            reason_code="delivery_user_ineligible",
            suppressed_at=NOW,
        )
    )

    assert result.status.value == "suppressed"
    assert result.reason_code == "delivery_user_ineligible"
    assert record.status == "suppressed"
    assert record.last_error_code == "delivery_user_ineligible"
    restarted = asyncio.run(
        start_delivery_attempt(
            _Session(_Result(record)),
            delivery_attempt_id=ATTEMPT_ID,
            started_at=NOW,
        )
    )
    assert restarted.can_execute is False


def test_provider_event_applies_delivered_and_clicked_without_state_regression(
    monkeypatch,
) -> None:
    import asyncio

    import src.market_morning.repositories.delivery_webhooks as repository

    delivery = _record(status="sent")
    delivery.provider_message_id = "provider-message-1"
    delivered_event = _provider_event_record()
    monkeypatch.setattr(repository, "new_id", lambda: EVENT_ROW_ID)

    delivered = asyncio.run(
        repository.record_delivery_provider_event(
            _Session(_Result(), _Result(delivered_event), _Result(delivery)),
            provider="resend",
            provider_event_id="event-1",
            provider_message_id="provider-message-1",
            event_type="delivered",
            payload_sha256="b" * 64,
            received_at=NOW,
        )
    )

    assert delivered.outcome.value == "applied"
    assert delivered.delivery_status.value == "delivered"
    assert delivery.status == "delivered"
    assert delivery.delivered_at == NOW.replace(tzinfo=None)

    clicked_event = _provider_event_record()
    clicked_event.provider_event_id = "event-2"
    clicked_event.event_type = "clicked"
    clicked_event.payload_sha256 = "c" * 64
    monkeypatch.setattr(
        repository,
        "new_id",
        lambda: "55555555-5555-4555-8555-555555555555",
    )
    clicked = asyncio.run(
        repository.record_delivery_provider_event(
            _Session(_Result(), _Result(clicked_event), _Result(delivery)),
            provider="resend",
            provider_event_id="event-2",
            provider_message_id="provider-message-1",
            event_type="clicked",
            payload_sha256="c" * 64,
            received_at=NOW,
        )
    )

    assert clicked.outcome.value == "applied"
    assert clicked.delivery_status.value == "clicked"
    assert delivery.status == "clicked"
    assert delivery.clicked_at == NOW.replace(tzinfo=None)


def test_provider_event_is_idempotent_and_marks_late_failure_stale(monkeypatch) -> None:
    import asyncio

    import src.market_morning.repositories.delivery_webhooks as repository

    already_processed = _provider_event_record(outcome="applied")
    already_processed.delivery_attempt_id = ATTEMPT_ID
    delivery = _record(status="delivered")
    delivery.provider_message_id = "provider-message-1"
    monkeypatch.setattr(repository, "new_id", lambda: EVENT_ROW_ID)

    duplicate = asyncio.run(
        repository.record_delivery_provider_event(
            _Session(_Result(), _Result(already_processed)),
            provider="resend",
            provider_event_id="event-1",
            provider_message_id="provider-message-1",
            event_type="delivered",
            payload_sha256="b" * 64,
            received_at=NOW,
        )
    )

    assert duplicate.duplicate is True
    assert duplicate.outcome.value == "applied"

    late_event = _provider_event_record()
    late_event.provider_event_id = "event-late"
    late_event.event_type = "failed"
    late_event.payload_sha256 = "d" * 64
    monkeypatch.setattr(
        repository,
        "new_id",
        lambda: "66666666-6666-4666-8666-666666666666",
    )
    stale = asyncio.run(
        repository.record_delivery_provider_event(
            _Session(_Result(), _Result(late_event), _Result(delivery)),
            provider="resend",
            provider_event_id="event-late",
            provider_message_id="provider-message-1",
            event_type="failed",
            payload_sha256="d" * 64,
            received_at=NOW,
        )
    )

    assert stale.outcome.value == "stale"
    assert stale.delivery_status.value == "delivered"
    assert delivery.status == "delivered"


def test_provider_event_records_unmatched_message_without_delivery_identity(
    monkeypatch,
) -> None:
    import asyncio

    import src.market_morning.repositories.delivery_webhooks as repository

    event = _provider_event_record()
    event.provider_message_id = "unknown-message"
    monkeypatch.setattr(repository, "new_id", lambda: EVENT_ROW_ID)
    result = asyncio.run(
        repository.record_delivery_provider_event(
            _Session(_Result(), _Result(event), _Result()),
            provider="resend",
            provider_event_id="event-1",
            provider_message_id="unknown-message",
            event_type="delivered",
            payload_sha256="b" * 64,
            received_at=NOW,
        )
    )

    assert result.outcome.value == "unmatched"
    assert result.delivery_attempt_id is None
    assert result.delivery_status is None
