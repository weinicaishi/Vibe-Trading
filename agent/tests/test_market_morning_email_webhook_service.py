"""Provider-agnostic, verified email webhook orchestration."""

from __future__ import annotations

import asyncio
import hashlib
from collections import deque
from datetime import datetime, timezone

import pytest

NOW = datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)
ATTEMPT_ID = "33333333-3333-4333-8333-333333333333"
RAW_BODY = b'{"id":"event-1","message_id":"provider-message-1"}'


class _Transaction:
    def __init__(self, session, entries):
        self.session = session
        self.entries = entries

    async def __aenter__(self):
        self.entries.append(("enter", self.session))
        return self.session

    async def __aexit__(self, exc_type, exc, traceback):
        self.entries.append(("rollback" if exc_type else "commit", self.session))


class _Factory:
    def __init__(self, *sessions):
        self.sessions = deque(sessions)
        self.entries = []

    def begin(self):
        return _Transaction(self.sessions.popleft(), self.entries)


def _normalized(*, event_type="delivered"):
    from src.market_morning.email_webhook_service import (
        NormalizedDeliveryProviderEvent,
    )

    return NormalizedDeliveryProviderEvent(
        provider="resend",
        provider_event_id="event-1",
        provider_message_id="provider-message-1",
        event_type=event_type,
    )


def test_webhook_verifies_and_parses_before_opening_transaction(monkeypatch) -> None:
    import src.market_morning.email_webhook_service as service
    from src.market_morning.repositories.delivery_webhooks import (
        DeliveryProviderEventOutcome,
        DeliveryProviderEventResult,
    )
    from src.market_morning.repositories.email_delivery import (
        DeliveryLifecycleStatus,
    )

    session = object()
    factory = _Factory(session)
    lifecycle = []

    async def verify(raw_body, headers):
        lifecycle.append(("verify", raw_body, headers))
        return True

    async def parse(raw_body, headers):
        lifecycle.append(("parse", raw_body, headers))
        return _normalized()

    async def record(active_session, **kwargs):
        lifecycle.append(("record", active_session, kwargs))
        return DeliveryProviderEventResult(
            provider_event_row_id="44444444-4444-4444-8444-444444444444",
            provider_event_id="event-1",
            outcome=DeliveryProviderEventOutcome.APPLIED,
            duplicate=False,
            delivery_attempt_id=ATTEMPT_ID,
            delivery_status=DeliveryLifecycleStatus.DELIVERED,
        )

    monkeypatch.setattr(service, "record_delivery_provider_event", record)
    result = asyncio.run(
        service.process_email_delivery_webhook(
            provider="resend",
            raw_body=RAW_BODY,
            headers={
                "svix-id": "event-1",
                "svix-signature": "secret-signature",
            },
            signature_verifier=verify,
            event_parser=parse,
            session_factory=factory,
            clock=lambda: NOW,
        )
    )

    assert [entry[0] for entry in lifecycle] == ["verify", "parse", "record"]
    assert lifecycle[1][2]["svix-id"] == "event-1"
    assert lifecycle[2][1] is session
    assert lifecycle[2][2]["payload_sha256"] == hashlib.sha256(RAW_BODY).hexdigest()
    assert "secret-signature" not in repr(result)
    assert result.delivery_status.value == "delivered"
    assert factory.entries == [("enter", session), ("commit", session)]


def test_webhook_rejects_invalid_signature_without_database_access() -> None:
    import src.market_morning.email_webhook_service as service

    factory = _Factory()

    with pytest.raises(service.EmailWebhookRejected) as error:
        asyncio.run(
            service.process_email_delivery_webhook(
                provider="resend",
                raw_body=RAW_BODY,
                headers={"svix-signature": "invalid secret"},
                signature_verifier=lambda *args: asyncio.sleep(0, result=False),
                event_parser=lambda *args: pytest.fail("parser must not run"),
                session_factory=factory,
                clock=lambda: NOW,
            )
        )

    assert error.value.error_code == "delivery_webhook_signature_invalid"
    assert "invalid secret" not in str(error.value)
    assert factory.entries == []


def test_webhook_rejects_provider_mismatch_without_database_access() -> None:
    import src.market_morning.email_webhook_service as service

    factory = _Factory()

    with pytest.raises(service.EmailWebhookRejected) as error:
        asyncio.run(
            service.process_email_delivery_webhook(
                provider="ses",
                raw_body=RAW_BODY,
                headers={},
                signature_verifier=lambda *args: asyncio.sleep(0, result=True),
                event_parser=lambda *args: asyncio.sleep(0, result=_normalized()),
                session_factory=factory,
                clock=lambda: NOW,
            )
        )

    assert error.value.error_code == "delivery_webhook_invalid"
    assert factory.entries == []
