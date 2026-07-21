"""Short-transaction orchestration for private reminder email delivery."""

from __future__ import annotations

import asyncio
import hashlib
from collections import deque
from datetime import date, datetime, timezone

import pytest

NOW = datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)
USER_ID = "11111111-1111-4111-8111-111111111111"
RUN_ID = "22222222-2222-4222-8222-222222222222"
ATTEMPT_ID = "33333333-3333-4333-8333-333333333333"
TOKEN = "signed-token"


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


def _context(*, eligible=True, reason=None):
    from src.market_morning.email_delivery_service import EmailDeliveryContext

    return EmailDeliveryContext(
        delivery_attempt_id=ATTEMPT_ID,
        user_id=USER_ID,
        external_subject="oidc-subject-1",
        edition_date=date(2026, 7, 21),
        global_run_id=RUN_ID,
        idempotency_key=(
            f"email:2026-07-21:{USER_ID}:global-run:{RUN_ID}"
        ),
        deep_link_token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(),
        eligible=eligible,
        suppression_reason=reason,
    )


def _start(*, can_execute=True, status="sending", attempt=1, retry=True):
    from src.market_morning.repositories.email_delivery import (
        DeliveryLifecycleStatus,
        DeliveryStartResult,
    )

    return DeliveryStartResult(
        delivery_attempt_id=ATTEMPT_ID,
        status=DeliveryLifecycleStatus(status),
        attempt_number=attempt,
        retry_permitted=retry,
        can_execute=can_execute,
    )


def test_email_service_rechecks_then_calls_ports_outside_transactions(monkeypatch) -> None:
    import src.market_morning.email_delivery_service as service
    from src.market_morning.repositories.email_delivery import (
        DeliveryCompletionResult,
        DeliveryLifecycleStatus,
    )

    first_session = object()
    complete_session = object()
    factory = _Factory(first_session, complete_session)
    lifecycle = []

    async def load(session, **kwargs):
        lifecycle.append(("load", session))
        return _context()

    async def start(session, **kwargs):
        lifecycle.append(("start", session))
        return _start()

    async def resolve(user_id, external_subject):
        lifecycle.append(("resolve", user_id, external_subject))
        return "private@example.jp"

    async def link(context):
        lifecycle.append(("link", context.delivery_attempt_id))
        return service.DeliveryDeepLink(
            url=f"https://app.example.jp/market-morning/open/{TOKEN}",
            token=TOKEN,
        )

    async def send(message):
        lifecycle.append(("send", message))
        return service.EmailProviderReceipt(provider_message_id="message-1")

    async def complete(session, **kwargs):
        lifecycle.append(("complete", session))
        return DeliveryCompletionResult(
            delivery_attempt_id=ATTEMPT_ID,
            status=DeliveryLifecycleStatus.SENT,
            provider_message_id="message-1",
        )

    monkeypatch.setattr(service, "load_email_delivery_context", load)
    monkeypatch.setattr(service, "start_delivery_attempt", start)
    monkeypatch.setattr(service, "complete_delivery_attempt", complete)

    result = asyncio.run(
        service.run_email_delivery(
            service.EmailDeliveryCommand(delivery_attempt_id=ATTEMPT_ID),
            identity_resolver=resolve,
            link_builder=link,
            provider=send,
            session_factory=factory,
            clock=lambda: NOW,
        )
    )

    assert result.status.value == "sent"
    assert result.provider_message_id == "message-1"
    assert lifecycle[:2] == [("load", first_session), ("start", first_session)]
    assert lifecycle[-1] == ("complete", complete_session)
    message = next(item[1] for item in lifecycle if item[0] == "send")
    assert message.destination == "private@example.jp"
    assert message.private_url.endswith(TOKEN)
    assert "準備できました" in message.subject
    assert "株" not in message.body and "買" not in message.body
    assert factory.entries == [
        ("enter", first_session),
        ("commit", first_session),
        ("enter", complete_session),
        ("commit", complete_session),
    ]


def test_email_service_suppresses_ineligible_user_before_any_port(monkeypatch) -> None:
    import src.market_morning.email_delivery_service as service
    from src.market_morning.repositories.email_delivery import (
        DeliveryLifecycleStatus,
        DeliverySuppressionResult,
    )

    session = object()
    factory = _Factory(session)
    suppressed = []

    monkeypatch.setattr(
        service,
        "load_email_delivery_context",
        lambda *args, **kwargs: asyncio.sleep(
            0,
            result=_context(
                eligible=False,
                reason="delivery_user_ineligible",
            ),
        ),
    )

    async def suppress(active_session, **kwargs):
        suppressed.append((active_session, kwargs))
        return DeliverySuppressionResult(
            delivery_attempt_id=ATTEMPT_ID,
            status=DeliveryLifecycleStatus.SUPPRESSED,
            reason_code=kwargs["reason_code"],
        )

    monkeypatch.setattr(service, "suppress_delivery_attempt", suppress)
    result = asyncio.run(
        service.run_email_delivery(
            service.EmailDeliveryCommand(delivery_attempt_id=ATTEMPT_ID),
            identity_resolver=lambda *args: pytest.fail("resolver must not run"),
            link_builder=lambda *args: pytest.fail("link builder must not run"),
            provider=lambda *args: pytest.fail("provider must not run"),
            session_factory=factory,
            clock=lambda: NOW,
        )
    )

    assert result.status.value == "suppressed"
    assert suppressed[0][0] is session
    assert suppressed[0][1]["reason_code"] == "delivery_user_ineligible"


def test_email_service_rejects_mismatched_private_token_without_sending(monkeypatch) -> None:
    import src.market_morning.email_delivery_service as service
    from src.market_morning.repositories.email_delivery import (
        DeliveryLifecycleStatus,
        DeliverySuppressionResult,
    )

    factory = _Factory(object(), object())
    suppressed = []
    monkeypatch.setattr(
        service,
        "load_email_delivery_context",
        lambda *args, **kwargs: asyncio.sleep(0, result=_context()),
    )
    monkeypatch.setattr(
        service,
        "start_delivery_attempt",
        lambda *args, **kwargs: asyncio.sleep(0, result=_start()),
    )

    async def suppress(session, **kwargs):
        suppressed.append(kwargs)
        return DeliverySuppressionResult(
            delivery_attempt_id=ATTEMPT_ID,
            status=DeliveryLifecycleStatus.SUPPRESSED,
            reason_code=kwargs["reason_code"],
        )

    monkeypatch.setattr(service, "suppress_delivery_attempt", suppress)
    result = asyncio.run(
        service.run_email_delivery(
            service.EmailDeliveryCommand(delivery_attempt_id=ATTEMPT_ID),
            identity_resolver=lambda *args: asyncio.sleep(
                0, result="private@example.jp"
            ),
            link_builder=lambda *args: asyncio.sleep(
                0,
                result=service.DeliveryDeepLink(
                    url="https://app.example.jp/market-morning/open/wrong",
                    token="wrong",
                ),
            ),
            provider=lambda *args: pytest.fail("provider must not run"),
            session_factory=factory,
            clock=lambda: NOW,
        )
    )

    assert result.status.value == "suppressed"
    assert suppressed[0]["reason_code"] == "delivery_link_invalid"


def test_email_service_suppresses_missing_destination_with_distinct_reason(
    monkeypatch,
) -> None:
    import src.market_morning.email_delivery_service as service
    from src.market_morning.repositories.email_delivery import (
        DeliveryLifecycleStatus,
        DeliverySuppressionResult,
    )

    factory = _Factory(object(), object())
    suppressed = []
    monkeypatch.setattr(
        service,
        "load_email_delivery_context",
        lambda *args, **kwargs: asyncio.sleep(0, result=_context()),
    )
    monkeypatch.setattr(
        service,
        "start_delivery_attempt",
        lambda *args, **kwargs: asyncio.sleep(0, result=_start()),
    )

    async def suppress(session, **kwargs):
        suppressed.append(kwargs)
        return DeliverySuppressionResult(
            delivery_attempt_id=ATTEMPT_ID,
            status=DeliveryLifecycleStatus.SUPPRESSED,
            reason_code=kwargs["reason_code"],
        )

    monkeypatch.setattr(service, "suppress_delivery_attempt", suppress)
    result = asyncio.run(
        service.run_email_delivery(
            service.EmailDeliveryCommand(delivery_attempt_id=ATTEMPT_ID),
            identity_resolver=lambda *args: asyncio.sleep(0, result="missing"),
            link_builder=lambda *args: pytest.fail("link builder must not run"),
            provider=lambda *args: pytest.fail("provider must not run"),
            session_factory=factory,
            clock=lambda: NOW,
        )
    )

    assert result.status.value == "suppressed"
    assert suppressed[0]["reason_code"] == "email_destination_unavailable"


def test_email_service_records_sanitized_provider_failure_and_retries(monkeypatch) -> None:
    import src.market_morning.email_delivery_service as service

    factory = _Factory(object(), object())
    failures = []
    monkeypatch.setattr(
        service,
        "load_email_delivery_context",
        lambda *args, **kwargs: asyncio.sleep(0, result=_context()),
    )
    monkeypatch.setattr(
        service,
        "start_delivery_attempt",
        lambda *args, **kwargs: asyncio.sleep(0, result=_start()),
    )

    async def fail(session, **kwargs):
        failures.append(kwargs)
        return type("Failure", (), {"retry_permitted": True})()

    async def provider(message):
        raise service.EmailDeliveryPortUnavailable("secret provider response")

    monkeypatch.setattr(service, "fail_delivery_attempt", fail)
    with pytest.raises(service.EmailDeliveryRetryable) as error:
        asyncio.run(
            service.run_email_delivery(
                service.EmailDeliveryCommand(delivery_attempt_id=ATTEMPT_ID),
                identity_resolver=lambda *args: asyncio.sleep(
                    0, result="private@example.jp"
                ),
                link_builder=lambda *args: asyncio.sleep(
                    0,
                    result=service.DeliveryDeepLink(
                        url=f"https://app.example.jp/market-morning/open/{TOKEN}",
                        token=TOKEN,
                    ),
                ),
                provider=provider,
                session_factory=factory,
                clock=lambda: NOW,
            )
        )

    assert error.value.error_code == "email_provider_unavailable"
    assert "secret provider response" not in str(error.value)
    assert failures[0]["error_code"] == "email_provider_unavailable"
