"""Settings, consent, deletion request, and telemetry policy tests."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import mysql

from src.market_morning.models import (
    AccountDeletionRequest,
    AnalyticsEvent,
    AuditLog,
    UserConsent,
)
from src.market_morning.settings import (
    ConsentMutationStatus,
    ConsentType,
    DeletionRequestStatus,
    SettingsValidationError,
    create_account_deletion_request,
    read_user_settings,
    set_consent_acceptance,
    update_user_settings,
)
from src.market_morning.telemetry import (
    AnalyticsEventName,
    AnalyticsValidationError,
    build_analytics_event,
)

USER_ID = "11111111-1111-4111-8111-111111111111"
NOW = datetime(2026, 7, 20, 23, 30, 0)


class _Result:
    def __init__(self, *, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, *results: _Result):
        self.results = deque(results)
        self.statements = []
        self.added = []
        self.flush_count = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.popleft()

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1


def _user(*, email_opt_in: bool = False, status: str = "active"):
    return SimpleNamespace(
        user_id=USER_ID,
        account_status=status,
        deleted_at=None,
        timezone="Asia/Tokyo",
        email_opt_in=email_opt_in,
        last_product_activity_at=NOW,
        updated_at=NOW,
    )


def _consent(
    *,
    consent_type: ConsentType = ConsentType.RISK_DISCLOSURE,
    version: str = "2026-07-01",
    revoked_at: datetime | None = None,
) -> UserConsent:
    return UserConsent(
        consent_id="22222222-2222-4222-8222-222222222222",
        user_id=USER_ID,
        consent_type=consent_type.value,
        consent_version=version,
        accepted_at=NOW,
        revoked_at=revoked_at,
    )


def test_telemetry_rejects_unregistered_or_sensitive_properties() -> None:
    with pytest.raises(AnalyticsValidationError, match="not registered"):
        build_analytics_event("arbitrary_event", user_id=USER_ID)

    for property_name in ("email", "query", "user_label", "source_url"):
        with pytest.raises(AnalyticsValidationError, match="disallowed"):
            build_analytics_event(
                AnalyticsEventName.WATCHLIST_ADDED,
                user_id=USER_ID,
                properties={property_name: "private-value"},
            )

    with pytest.raises(AnalyticsValidationError, match="not allowed"):
        build_analytics_event(
            AnalyticsEventName.SOURCE_OPENED,
            user_id=USER_ID,
            properties={"source_kind": "person@example.com"},
        )


def test_telemetry_accepts_only_the_safe_event_contract() -> None:
    event = build_analytics_event(
        AnalyticsEventName.ISSUER_SEARCH_PERFORMED,
        user_id=USER_ID,
        properties={
            "matched": True,
            "query_kind": "issuer_code",
            "query_length": 4,
            "result_count": 1,
        },
        request_id="request-123",
        occurred_at=NOW,
    )

    assert event.event_name == "issuer_search_performed"
    assert event.properties["query_length"] == 4
    assert "query" not in event.properties


def test_update_settings_is_locked_audited_and_privacy_safe() -> None:
    user = _user(email_opt_in=False)
    session = _Session(_Result(scalar=user))

    result = asyncio.run(
        update_user_settings(
            session,
            user_id=USER_ID,
            email_opt_in=True,
            now=NOW,
        )
    )

    assert result.email_opt_in is True
    audit = next(value for value in session.added if isinstance(value, AuditLog))
    event = next(value for value in session.added if isinstance(value, AnalyticsEvent))
    assert audit.details == {"changed_fields": ["email_opt_in"]}
    assert event.properties == {"email_opt_in": True}
    assert "email_address" not in event.properties
    assert "@" not in str(event.properties)
    assert session.flush_count == 1
    sql = str(session.statements[0].compile(dialect=mysql.dialect()))
    assert "FOR UPDATE" in sql


def test_update_settings_rejects_non_jst_timezone_before_database_access() -> None:
    session = _Session()

    with pytest.raises(SettingsValidationError, match="Asia/Tokyo"):
        asyncio.run(
            update_user_settings(
                session,
                user_id=USER_ID,
                timezone="UTC",
                now=NOW,
            )
        )

    assert session.statements == []


def test_consent_acceptance_is_versioned_audited_and_idempotent() -> None:
    user = _user(email_opt_in=True)
    create_session = _Session(_Result(scalar=user), _Result(scalar=None))
    created = asyncio.run(
        set_consent_acceptance(
            create_session,
            user_id=USER_ID,
            consent_type=ConsentType.RISK_DISCLOSURE,
            consent_version="2026-07-01",
            accepted=True,
            now=NOW,
        )
    )

    assert created.status == ConsentMutationStatus.ACCEPTED
    consent = next(
        value for value in create_session.added if isinstance(value, UserConsent)
    )
    event = next(
        value for value in create_session.added if isinstance(value, AnalyticsEvent)
    )
    assert consent.consent_version == "2026-07-01"
    assert event.properties["consent_type"] == "risk_disclosure"

    existing = _consent()
    repeat_session = _Session(_Result(scalar=_user()), _Result(scalar=existing))
    repeated = asyncio.run(
        set_consent_acceptance(
            repeat_session,
            user_id=USER_ID,
            consent_type=ConsentType.RISK_DISCLOSURE,
            consent_version="2026-07-01",
            accepted=True,
            now=NOW,
        )
    )

    assert repeated.status == ConsentMutationStatus.ALREADY_ACCEPTED
    assert repeat_session.added == []
    assert repeat_session.flush_count == 0


def test_consent_version_rejects_free_text_or_personal_data() -> None:
    session = _Session()

    with pytest.raises(SettingsValidationError, match="safe version identifier"):
        asyncio.run(
            set_consent_acceptance(
                session,
                user_id=USER_ID,
                consent_type=ConsentType.DATA_DISCLOSURE,
                consent_version="person@example.com",
                accepted=True,
                now=NOW,
            )
        )

    assert session.statements == []


def test_read_settings_returns_current_active_consents() -> None:
    risk = _consent()
    session = _Session(_Result(scalar=_user()), _Result(rows=[risk]))

    result = asyncio.run(read_user_settings(session, user_id=USER_ID))

    assert result.timezone == "Asia/Tokyo"
    assert result.risk_disclosure.accepted is True
    assert result.risk_disclosure.consent_version == "2026-07-01"
    assert result.data_disclosure.accepted is False


def test_deletion_request_is_pending_audited_and_idempotent(monkeypatch) -> None:
    import src.market_morning.settings as settings

    async def enqueue(session, **kwargs):
        return SimpleNamespace(status="enqueued")

    monkeypatch.setattr(settings, "enqueue_job", enqueue)
    user = _user(email_opt_in=True)
    create_session = _Session(_Result(scalar=user), _Result(scalar=None))
    created = asyncio.run(
        create_account_deletion_request(
            create_session,
            user_id=USER_ID,
            now=NOW,
        )
    )

    assert created.status == DeletionRequestStatus.REQUESTED
    request = next(
        value
        for value in create_session.added
        if isinstance(value, AccountDeletionRequest)
    )
    event = next(
        value for value in create_session.added if isinstance(value, AnalyticsEvent)
    )
    assert request.status == "pending"
    assert event.properties == {"request_status": "pending"}
    assert user.account_status == "deletion_pending"
    assert user.email_opt_in is False
    assert user.last_product_activity_at is None

    repeat_session = _Session(
        _Result(scalar=_user(status="deletion_pending")),
        _Result(scalar=request),
    )
    repeated = asyncio.run(
        create_account_deletion_request(
            repeat_session,
            user_id=USER_ID,
            now=NOW,
        )
    )

    assert repeated.status == DeletionRequestStatus.ALREADY_REQUESTED
    assert repeated.request_id == request.request_id
    assert repeat_session.added == []


def test_deletion_request_enqueues_one_idempotent_background_job(monkeypatch) -> None:
    import src.market_morning.settings as settings

    calls = []

    async def enqueue(session, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status="enqueued")

    monkeypatch.setattr(settings, "enqueue_job", enqueue)
    session = _Session(_Result(scalar=_user()), _Result(scalar=None))

    result = asyncio.run(
        create_account_deletion_request(session, user_id=USER_ID, now=NOW)
    )

    assert result.status == DeletionRequestStatus.REQUESTED
    request = next(
        value for value in session.added if isinstance(value, AccountDeletionRequest)
    )
    assert calls == [
        {
            "job_type": settings.MarketMorningJobType.ACCOUNT_DELETION,
            "idempotency_key": f"account-deletion:{request.request_id}",
            "payload": {"schema_version": 1, "request_id": request.request_id},
            "priority": 100,
            "available_at": NOW.replace(tzinfo=settings.timezone.utc),
            "max_attempts": 5,
            "created_at": NOW.replace(tzinfo=settings.timezone.utc),
        }
    ]
