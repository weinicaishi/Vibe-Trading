"""Authenticated export and asynchronous account-deletion tests."""

from __future__ import annotations

import asyncio
import sys
from collections import deque
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import OperationalError

from src.market_morning.account_privacy import (
    AccountDeletionProcessStatus,
    build_account_export_statements,
    export_account_data,
    process_account_deletion,
)
from src.market_morning.models import AccountDeletionRequest, AuditLog


NOW = datetime(2026, 7, 21, 1, 30, 0)
USER_ID = "11111111-1111-4111-8111-111111111111"
REQUEST_ID = "22222222-2222-4222-8222-222222222222"


class _Result:
    def __init__(self, *, scalar=None, mappings=()):
        self.scalar = scalar
        self.mapping_rows = tuple(mappings)

    def scalar_one_or_none(self):
        return self.scalar

    def mappings(self):
        return SimpleNamespace(all=lambda: self.mapping_rows)


class _Session:
    def __init__(self, *results):
        self.results = deque(results)
        self.statements = []
        self.added = []
        self.flush_count = 0

    async def execute(self, statement):
        self.statements.append(statement)
        if self.results:
            return self.results.popleft()
        return _Result()

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1


def _mysql_sql(statement) -> str:
    return str(
        statement.compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def _user(*, status: str = "active"):
    return SimpleNamespace(
        user_id=USER_ID,
        external_subject="oidc|subject",
        account_status=status,
        trial_or_subscription_status="private_beta",
        timezone="Asia/Tokyo",
        email_opt_in=True,
        last_product_activity_at=NOW,
        created_at=NOW,
        updated_at=NOW,
        deleted_at=None,
    )


def _request(*, status: str = "pending"):
    return AccountDeletionRequest(
        request_id=REQUEST_ID,
        user_id=USER_ID,
        status=status,
        requested_at=NOW,
        updated_at=NOW,
    )


def test_export_queries_cover_user_owned_data_without_operational_secrets() -> None:
    statements = build_account_export_statements(user_id=USER_ID)
    sql = "\n".join(_mysql_sql(statement) for _, statement in statements)
    sections = {name for name, _ in statements}

    assert sections == {
        "account",
        "consents",
        "watchlist",
        "editions",
        "edition_event_states",
        "source_opens",
        "research_notes",
        "deliveries",
        "analytics",
        "audit_history",
        "deletion_requests",
        "private_beta_invites",
    }
    assert "mm_users.external_subject" in sql
    assert "mm_issuer_research_notes.note_text" in sql
    assert "mm_morning_editions.payload" in sql
    for forbidden in (
        "deep_link_token_sha256",
        "provider_message_id",
        "payload_sha256",
        "token_sha256",
        "mm_jobs.payload",
    ):
        assert forbidden not in sql


def test_export_returns_structured_complete_sections_for_active_user() -> None:
    section_rows = [
        ({"user_id": USER_ID, "external_subject": "oidc|subject"},),
        (),
        ({"issuer_code": "7203", "user_label": "長期"},),
        ({"edition_date": date(2026, 7, 21), "payload": {"status": "published"}},),
        (),
        (),
        ({"issuer_code": "7203", "note_text": "確認する"},),
        (),
        (),
        (),
        (),
        (),
    ]
    session = _Session(
        _Result(scalar=_user()),
        *(_Result(mappings=rows) for rows in section_rows),
    )

    result = asyncio.run(export_account_data(session, user_id=USER_ID, now=NOW))

    assert result.schema_version == 1
    assert result.generated_at == NOW
    assert result.data["account"][0]["external_subject"] == "oidc|subject"
    assert result.data["research_notes"][0]["note_text"] == "確認する"
    assert result.data["watchlist"][0]["issuer_code"] == "7203"


def test_deletion_anonymizes_identity_and_removes_all_private_child_data() -> None:
    request = _request()
    user = _user()
    session = _Session(
        _Result(scalar=request),
        _Result(scalar=user),
        _Result(scalar=None),
    )

    result = asyncio.run(
        process_account_deletion(
            session,
            request_id=REQUEST_ID,
            actor_reference="market-morning-deletion-worker",
            now=NOW,
        )
    )

    sql = "\n".join(_mysql_sql(statement) for statement in session.statements[3:])
    assert result.status == AccountDeletionProcessStatus.COMPLETED
    assert request.status == "completed"
    assert request.completed_at == NOW
    assert user.account_status == "deleted"
    assert user.deleted_at == NOW
    assert user.email_opt_in is False
    assert user.last_product_activity_at is None
    assert user.external_subject.startswith("deleted:")
    assert "oidc|subject" not in user.external_subject
    for table in (
        "mm_user_consents",
        "mm_watchlist_items",
        "mm_morning_editions",
        "mm_edition_event_states",
        "mm_edition_source_opens",
        "mm_issuer_research_notes",
        "mm_delivery_attempts",
        "mm_jobs",
    ):
        assert table in sql
    assert "UPDATE mm_analytics_events SET user_id=NULL" in sql
    assert "UPDATE mm_audit_logs SET actor_user_id=NULL" in sql
    assert "UPDATE mm_private_beta_invites SET accepted_by_user_id=NULL" in sql
    assert "mm_source_records" not in sql
    assert "mm_normalized_events" not in sql

    audit = next(value for value in session.added if isinstance(value, AuditLog))
    assert audit.actor_user_id is None
    assert audit.details == {
        "actor_reference": "market-morning-deletion-worker",
        "status": "completed",
    }
    assert "oidc|subject" not in repr(audit.details)


def test_completed_deletion_is_idempotent_and_does_not_repeat_destructive_sql() -> None:
    request = _request(status="completed")
    request.completed_at = NOW
    session = _Session(_Result(scalar=request))

    result = asyncio.run(
        process_account_deletion(
            session,
            request_id=REQUEST_ID,
            actor_reference="market-morning-deletion-worker",
            now=NOW,
        )
    )

    assert result.status == AccountDeletionProcessStatus.ALREADY_COMPLETED
    assert len(session.statements) == 1
    assert session.added == []


def test_deletion_job_handler_validates_payload_and_classifies_database_failure() -> None:
    from src.market_morning.jobs.handlers import make_account_deletion_handler
    from src.market_morning.jobs.worker import PermanentJobError, RetryableJobError

    calls = []

    async def runner(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            status=AccountDeletionProcessStatus.COMPLETED,
            request_id=REQUEST_ID,
            user_id=USER_ID,
            completed_at=NOW,
        )

    handler = make_account_deletion_handler(runner=runner)
    result = asyncio.run(
        handler({"schema_version": 1, "request_id": REQUEST_ID})
    )

    assert calls == [
        {
            "request_id": REQUEST_ID,
            "actor_reference": "market-morning-deletion-worker",
        }
    ]
    assert result == {
        "request_id": REQUEST_ID,
        "user_id": USER_ID,
        "deletion_status": "completed",
        "completed_at": NOW.isoformat(),
    }

    with pytest.raises(PermanentJobError, match="invalid_job_payload"):
        asyncio.run(handler({"schema_version": 1, "request_id": "not-a-uuid"}))

    async def database_down(**kwargs):
        raise OperationalError("SELECT 1", {}, ConnectionError("secret DSN"))

    with pytest.raises(RetryableJobError) as raised:
        asyncio.run(make_account_deletion_handler(runner=database_down)(
            {"schema_version": 1, "request_id": REQUEST_ID}
        ))
    assert raised.value.error_code == "account_deletion_database_unavailable"
    assert "secret DSN" not in str(raised.value)

    from src.market_morning.account_privacy import AccountPrivacyRetryable

    async def user_job_running(**kwargs):
        raise AccountPrivacyRetryable("user-scoped job is still running")

    with pytest.raises(RetryableJobError) as retrying:
        asyncio.run(make_account_deletion_handler(runner=user_job_running)(
            {"schema_version": 1, "request_id": REQUEST_ID}
        ))
    assert retrying.value.error_code == "account_deletion_user_job_running"


def test_default_handler_registry_always_contains_account_deletion_worker() -> None:
    from src.market_morning.jobs.handlers import build_job_handlers
    from src.market_morning.repositories.jobs import MarketMorningJobType

    registry = build_job_handlers(adapters={})

    assert MarketMorningJobType.ACCOUNT_DELETION in registry


def test_product_data_export_route_uses_only_authenticated_principal(
    monkeypatch,
) -> None:
    from src.api import market_morning_routes
    from src.api.market_morning_auth import (
        MarketMorningPrincipal,
        require_market_morning_principal,
    )
    from src.market_morning.account_privacy import AccountDataExport

    async def principal() -> MarketMorningPrincipal:
        return MarketMorningPrincipal(
            user_id=USER_ID,
            external_subject="oidc|subject",
        )

    async def allow_internal_request() -> None:
        return None

    monkeypatch.setitem(
        sys.modules,
        "api_server",
        SimpleNamespace(require_auth=allow_internal_request),
    )

    calls = []

    async def export(*, user_id: str):
        calls.append(user_id)
        return AccountDataExport(
            schema_version=1,
            generated_at=NOW,
            data={"account": [{"user_id": user_id}]},
        )

    monkeypatch.setattr(market_morning_routes, "get_account_data_export", export)
    app = FastAPI()
    market_morning_routes.register_market_morning_routes(app)
    app.dependency_overrides[require_market_morning_principal] = principal
    client = TestClient(app, client=("127.0.0.1", 50000))

    response = client.get("/market-morning/account-data-export")

    assert response.status_code == 200
    assert calls == [USER_ID]
    assert response.json()["schema_version"] == 1
    assert response.json()["data"]["account"][0]["user_id"] == USER_ID
