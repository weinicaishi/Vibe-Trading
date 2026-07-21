"""Tests for strict Resend runner assembly used by deployment factories."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
import hashlib
import json
from types import SimpleNamespace
import sys

import httpx
import pytest

USER_ID = "11111111-1111-4111-8111-111111111111"
RUN_ID = "22222222-2222-4222-8222-222222222222"
ATTEMPT_ID = "33333333-3333-4333-8333-333333333333"
SUBJECT = "oidc:" + "a" * 64
EDITION_DATE = date(2026, 7, 21)
NOW = datetime(2026, 7, 21, 0, 30, tzinfo=timezone.utc)


class _SessionFactory:
    def begin(self):
        raise AssertionError("orchestration functions are stubbed in this test")


def _environment(factory_path: str) -> dict[str, str]:
    return {
        "VIBE_MARKET_MORNING_EMAIL_IDENTITY_FACTORY": factory_path,
        "VIBE_MARKET_MORNING_DELIVERY_LINK_BASE_URL": (
            "https://morning.example.jp/market-morning"
        ),
        "VIBE_MARKET_MORNING_DELIVERY_LINK_SIGNING_SECRET": "s" * 32,
        "VIBE_MARKET_MORNING_RESEND_API_KEY": "re_private_test_key",
        "VIBE_MARKET_MORNING_RESEND_FROM": (
            "Market Morning <morning@example.jp>"
        ),
    }


def _install_identity_adapter(monkeypatch: pytest.MonkeyPatch) -> str:
    from src.market_morning.email_identity import (
        MarketMorningEmailIdentityAdapter,
        VerifiedEmailIdentity,
        clear_email_identity_adapter_cache,
    )

    async def resolve(user_id: str, external_subject: str):
        return VerifiedEmailIdentity(
            user_id=user_id,
            external_subject=external_subject,
            email="private.user@example.jp",
            email_verified=True,
            active=True,
        )

    module_name = "test_market_morning_email_runtime_identity"
    monkeypatch.setitem(
        sys.modules,
        module_name,
        SimpleNamespace(
            build=lambda: MarketMorningEmailIdentityAdapter(
                provider="test-directory",
                resolve_identity=resolve,
            )
        ),
    )
    clear_email_identity_adapter_cache()
    return f"{module_name}:build"


def test_runtime_runners_share_identity_signer_provider_and_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.market_morning.email_delivery_service import (
        EmailDeliveryCommand,
        EmailDeliveryContext,
        EmailProviderReceipt,
        EmailReminderMessage,
    )
    from src.market_morning.email_dispatch_service import EmailDispatchCommand
    from src.market_morning import email_runtime

    captured: dict[str, object] = {}

    async def fake_delivery(command, **kwargs):
        captured["delivery_command"] = command
        captured["delivery"] = kwargs
        return "delivery-result"

    async def fake_dispatch(command, **kwargs):
        captured["dispatch_command"] = command
        captured["dispatch"] = kwargs
        return "dispatch-result"

    monkeypatch.setattr(email_runtime, "run_email_delivery", fake_delivery)
    monkeypatch.setattr(email_runtime, "dispatch_email_deliveries", fake_dispatch)

    provider_requests: list[httpx.Request] = []

    def provider_handler(request: httpx.Request) -> httpx.Response:
        provider_requests.append(request)
        return httpx.Response(201, json={"id": "resend-message-1"})

    factory_path = _install_identity_adapter(monkeypatch)
    session_factory = _SessionFactory()
    runners = email_runtime.build_resend_email_runtime_runners(
        session_factory=session_factory,
        environ=_environment(factory_path),
        transport=httpx.MockTransport(provider_handler),
        clock=lambda: NOW,
    )

    delivery_result = asyncio.run(
        runners.email_delivery_runner(EmailDeliveryCommand(ATTEMPT_ID))
    )
    dispatch_result = asyncio.run(
        runners.email_dispatch_runner(
            EmailDispatchCommand(
                global_run_id=RUN_ID,
                edition_date=EDITION_DATE,
                dispatched_at=NOW,
            )
        )
    )

    assert delivery_result == "delivery-result"
    assert dispatch_result == "dispatch-result"
    assert captured["delivery_command"].delivery_attempt_id == ATTEMPT_ID
    assert captured["dispatch_command"].global_run_id == RUN_ID
    delivery_ports = captured["delivery"]
    dispatch_ports = captured["dispatch"]
    assert delivery_ports["session_factory"] is session_factory
    assert dispatch_ports["session_factory"] is session_factory
    assert asyncio.run(delivery_ports["identity_resolver"](USER_ID, SUBJECT)) == (
        "private.user@example.jp"
    )

    digest = asyncio.run(
        dispatch_ports["token_digest_builder"](USER_ID, RUN_ID, EDITION_DATE)
    )
    context = EmailDeliveryContext(
        delivery_attempt_id=ATTEMPT_ID,
        user_id=USER_ID,
        external_subject=SUBJECT,
        edition_date=EDITION_DATE,
        global_run_id=RUN_ID,
        idempotency_key="email-runtime-test",
        deep_link_token_sha256=digest,
        eligible=True,
        suppression_reason=None,
    )
    link = asyncio.run(delivery_ports["link_builder"](context))
    assert hashlib.sha256(link.token.encode("ascii")).hexdigest() == digest
    assert link.url.endswith(f"#token={link.token}")

    receipt = asyncio.run(
        delivery_ports["provider"](
            EmailReminderMessage(
                destination="private.user@example.jp",
                private_url=link.url,
                idempotency_key="email-runtime-test",
            )
        )
    )
    assert receipt == EmailProviderReceipt(provider_message_id="resend-message-1")
    assert len(provider_requests) == 1
    assert json.loads(provider_requests[0].content)["to"] == [
        "private.user@example.jp"
    ]
    assert repr(runners) == "<ResendEmailRuntimeRunners configured>"
    assert "re_private_test_key" not in repr(runners)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (
            lambda values: values.pop(
                "VIBE_MARKET_MORNING_EMAIL_IDENTITY_FACTORY"
            ),
            "email_identity_factory_missing",
        ),
        (
            lambda values: values.pop(
                "VIBE_MARKET_MORNING_DELIVERY_LINK_SIGNING_SECRET"
            ),
            "delivery_link_configuration_invalid",
        ),
        (
            lambda values: values.pop("VIBE_MARKET_MORNING_RESEND_API_KEY"),
            "resend_api_key_invalid",
        ),
    ],
)
def test_runtime_runner_assembly_fails_closed_on_incomplete_configuration(
    monkeypatch: pytest.MonkeyPatch,
    mutation,
    expected_code: str,
) -> None:
    from src.market_morning.email_runtime import (
        build_resend_email_runtime_runners,
    )

    values = _environment(_install_identity_adapter(monkeypatch))
    mutation(values)
    with pytest.raises(Exception) as caught:
        build_resend_email_runtime_runners(
            session_factory=_SessionFactory(),
            environ=values,
        )
    assert str(caught.value) == expected_code
    assert "re_private_test_key" not in str(caught.value)


def test_runtime_runner_assembly_rejects_invalid_process_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.market_morning.email_runtime import (
        EmailRuntimeConfigurationError,
        build_resend_email_runtime_runners,
    )

    values = _environment(_install_identity_adapter(monkeypatch))
    for kwargs, expected_code in (
        ({"session_factory": None}, "email_runtime_session_factory_invalid"),
        (
            {"session_factory": _SessionFactory(), "clock": None},
            "email_runtime_clock_invalid",
        ),
    ):
        with pytest.raises(EmailRuntimeConfigurationError) as caught:
            build_resend_email_runtime_runners(environ=values, **kwargs)
        assert caught.value.error_code == expected_code
