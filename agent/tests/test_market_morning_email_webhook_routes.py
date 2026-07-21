"""Production-facing HTTP boundary for provider delivery callbacks."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _enabled_market_morning(monkeypatch: pytest.MonkeyPatch):
    from src.api.market_morning_email_webhook_routes import (
        clear_configured_adapter_cache,
    )
    from src.config.accessor import reset_env_config

    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    clear_configured_adapter_cache()
    reset_env_config()
    yield
    clear_configured_adapter_cache()
    reset_env_config()


def _adapter():
    from src.api.market_morning_email_webhook_routes import EmailWebhookAdapter

    async def verify(_raw_body, _headers):
        return True

    async def parse(_raw_body, _headers):
        raise AssertionError("route tests replace the orchestration service")

    return EmailWebhookAdapter(
        provider="resend",
        signature_verifier=verify,
        event_parser=parse,
    )


def _client(*, resolver=None) -> TestClient:
    from src.api.market_morning_email_webhook_routes import (
        register_market_morning_email_webhook_routes,
    )

    app = FastAPI()
    register_market_morning_email_webhook_routes(
        app,
        adapter_resolver=resolver,
    )
    return TestClient(app, client=("127.0.0.1", 50000))


def test_valid_callback_passes_exact_body_and_headers_to_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api import market_morning_email_webhook_routes as routes

    calls = []

    async def process(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(duplicate=False)

    monkeypatch.setattr(routes, "process_email_delivery_webhook", process)
    adapter = _adapter()
    client = _client(
        resolver=lambda provider: adapter if provider == "resend" else None
    )
    raw_body = b'{"id":"event-1","private":"must-not-leak"}'

    response = client.post(
        "/market-morning/webhooks/email/resend",
        content=raw_body,
        headers={"svix-signature": "secret-signature"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert len(calls) == 1
    assert calls[0]["provider"] == "resend"
    assert calls[0]["raw_body"] == raw_body
    assert calls[0]["headers"]["svix-signature"] == "secret-signature"
    assert calls[0]["signature_verifier"] is adapter.signature_verifier
    assert calls[0]["event_parser"] is adapter.event_parser
    assert "must-not-leak" not in response.text
    assert "secret-signature" not in response.text


def test_duplicate_callback_is_acknowledged_without_exposing_event_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api import market_morning_email_webhook_routes as routes

    async def process(**_kwargs):
        return SimpleNamespace(duplicate=True, provider_event_id="private-event")

    monkeypatch.setattr(routes, "process_email_delivery_webhook", process)
    client = _client(resolver=lambda _provider: _adapter())

    response = client.post(
        "/market-morning/webhooks/email/resend",
        content=b'{"id":"event-1"}',
    )

    assert response.status_code == 200
    assert response.json() == {"status": "duplicate"}
    assert "private-event" not in response.text


def test_unknown_provider_fails_closed_before_orchestration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api import market_morning_email_webhook_routes as routes

    async def must_not_run(**_kwargs):
        raise AssertionError("unconfigured providers must not be processed")

    monkeypatch.setattr(
        routes,
        "process_email_delivery_webhook",
        must_not_run,
    )
    client = _client(resolver=lambda _provider: None)

    response = client.post(
        "/market-morning/webhooks/email/unknown",
        content=b'{"id":"event-1"}',
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Email delivery webhook provider not found"


def test_disabled_product_fails_closed_before_adapter_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.config.accessor import reset_env_config

    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "false")
    reset_env_config()
    calls = []
    client = _client(resolver=lambda provider: calls.append(provider))

    response = client.post(
        "/market-morning/webhooks/email/resend",
        content=b'{"id":"event-1"}',
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Market Morning is disabled"
    assert calls == []


def test_oversized_body_is_rejected_before_orchestration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api import market_morning_email_webhook_routes as routes
    from src.market_morning.email_webhook_service import MAX_WEBHOOK_BODY_BYTES

    async def must_not_run(**_kwargs):
        raise AssertionError("oversized payloads must not be processed")

    monkeypatch.setattr(
        routes,
        "process_email_delivery_webhook",
        must_not_run,
    )
    client = _client(resolver=lambda _provider: _adapter())

    response = client.post(
        "/market-morning/webhooks/email/resend",
        content=b"x" * (MAX_WEBHOOK_BODY_BYTES + 1),
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "Email delivery webhook body is too large"


@pytest.mark.parametrize(
    ("error_code", "expected_status", "expected_detail"),
    [
        (
            "delivery_webhook_signature_invalid",
            401,
            "Email delivery webhook authentication failed",
        ),
        (
            "delivery_webhook_invalid",
            400,
            "Email delivery webhook payload is invalid",
        ),
    ],
)
def test_rejected_callback_uses_sanitized_http_errors(
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
    expected_status: int,
    expected_detail: str,
) -> None:
    from src.api import market_morning_email_webhook_routes as routes
    from src.market_morning.email_webhook_service import EmailWebhookRejected

    async def reject(**_kwargs):
        raise EmailWebhookRejected(error_code)

    monkeypatch.setattr(routes, "process_email_delivery_webhook", reject)
    client = _client(resolver=lambda _provider: _adapter())
    raw_body = b'{"private":"must-not-leak"}'

    response = client.post(
        "/market-morning/webhooks/email/resend",
        content=raw_body,
        headers={"svix-signature": "secret-signature"},
    )

    assert response.status_code == expected_status
    assert response.json()["detail"] == expected_detail
    assert "must-not-leak" not in response.text
    assert "secret-signature" not in response.text


def test_unexpected_processing_failure_is_sanitized_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api import market_morning_email_webhook_routes as routes

    async def fail(**_kwargs):
        raise RuntimeError("database password and private payload")

    monkeypatch.setattr(routes, "process_email_delivery_webhook", fail)
    client = _client(resolver=lambda _provider: _adapter())

    response = client.post(
        "/market-morning/webhooks/email/resend",
        content=b'{"private":"must-not-leak"}',
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Email delivery webhook is temporarily unavailable"
    )
    assert "password" not in response.text
    assert "must-not-leak" not in response.text


def test_default_resolver_requires_a_configured_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api import market_morning_email_webhook_routes as routes
    from src.config.accessor import reset_env_config

    monkeypatch.delenv(
        "VIBE_MARKET_MORNING_EMAIL_WEBHOOK_FACTORY",
        raising=False,
    )
    reset_env_config()
    client = _client()

    response = client.post(
        "/market-morning/webhooks/email/resend",
        content=b'{"id":"event-1"}',
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Email delivery webhook is temporarily unavailable"
    )
    assert routes.configured_adapter_cache_info().currsize == 0


def test_default_resolver_uses_the_configured_deployment_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api import market_morning_email_webhook_routes as routes
    from src.config.accessor import reset_env_config

    async def process(**_kwargs):
        return SimpleNamespace(duplicate=False)

    monkeypatch.setattr(routes, "process_email_delivery_webhook", process)
    monkeypatch.setitem(
        sys.modules,
        "deployment.market_morning",
        SimpleNamespace(build_webhooks=lambda: {"resend": _adapter()}),
    )
    monkeypatch.setenv(
        "VIBE_MARKET_MORNING_EMAIL_WEBHOOK_FACTORY",
        "deployment.market_morning:build_webhooks",
    )
    reset_env_config()
    client = _client()

    response = client.post(
        "/market-morning/webhooks/email/resend",
        content=b'{"id":"event-1"}',
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert routes.configured_adapter_cache_info().currsize == 1


def test_deployment_factory_contract_loads_provider_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api import market_morning_email_webhook_routes as routes

    adapter = _adapter()
    deployment_module = SimpleNamespace(build_webhooks=lambda: {"resend": adapter})
    monkeypatch.setitem(
        sys.modules,
        "deployment.market_morning",
        deployment_module,
    )
    routes.clear_configured_adapter_cache()

    adapters = routes.load_email_webhook_adapters(
        "deployment.market_morning:build_webhooks"
    )

    assert adapters == {"resend": adapter}


@pytest.mark.parametrize(
    "factory_path",
    ["", "missing_separator", "too:many:separators", ":factory", "module:"],
)
def test_deployment_factory_path_validation_is_fail_closed(factory_path: str) -> None:
    from src.api.market_morning_email_webhook_routes import (
        EmailWebhookConfigurationError,
        load_email_webhook_adapters,
    )

    with pytest.raises(EmailWebhookConfigurationError) as error:
        load_email_webhook_adapters(factory_path)

    assert error.value.error_code == "email_webhook_factory_invalid"
