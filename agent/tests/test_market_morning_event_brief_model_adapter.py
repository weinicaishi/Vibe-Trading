"""Production OpenAI-compatible model adapter for EventBrief generation."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

EVENT_ID = "11111111-1111-4111-8111-111111111111"
SOURCE_ID = "22222222-2222-4222-8222-222222222222"
OCCURRED_AT = datetime(2026, 7, 21, 0, 30, tzinfo=timezone.utc)
MODEL = "openai/gpt-example"


def _payload() -> dict:
    return {
        "schema_version": 1,
        "event_id": EVENT_ID,
        "event_version": 1,
        "title": "業績予想の修正",
        "occurred_at": OCCURRED_AT.isoformat(),
        "confirmed_facts": [
            {"text": "通期予想を10%修正した", "source_ids": [SOURCE_ID]}
        ],
        "open_questions": [],
        "source_ids": [SOURCE_ID],
        "model_version": MODEL,
        "review_status": "pending",
    }


def _model_input(*, evidence_text: str = "通期予想を10%修正した"):
    from src.market_morning.event_brief_service import EventBriefModelInput
    from src.market_morning.event_briefs import EventBriefSource

    return EventBriefModelInput(
        event_id=EVENT_ID,
        event_version=1,
        title="業績予想の修正",
        occurred_at=OCCURRED_AT,
        lifecycle_status="active",
        sources=(
            EventBriefSource(
                source_id=SOURCE_ID,
                provider="tdnet",
                original_url="https://example.com/disclosure.pdf",
                reachable=True,
                evidence_text=evidence_text,
            ),
        ),
    )


def _provider_response(
    *,
    content: str | None = None,
    finish_reason: str = "stop",
    include_usage: bool = True,
    include_cost: bool = True,
    response_model: str = MODEL,
) -> dict:
    response = {
        "id": "gen-secret-123",
        "object": "chat.completion",
        "created": 1784593800,
        "model": response_model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content or json.dumps(_payload(), ensure_ascii=False),
                    "refusal": None,
                },
                "finish_reason": finish_reason,
                "logprobs": None,
            }
        ],
        "system_fingerprint": "fp_example",
    }
    if include_usage:
        response["usage"] = {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "total_tokens": 150,
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 0},
        }
        if include_cost:
            response["usage"]["cost"] = 0.000175
    return response


def _adapter(handler, **overrides):
    from src.market_morning.event_brief_model_adapter import (
        OpenAICompatibleEventBriefGenerator,
    )

    options = {
        "provider": "openrouter",
        "model": MODEL,
        "endpoint_url": "https://openrouter.example/api/v1/chat/completions",
        "api_key": "provider-secret-key",
        "cost_currency": "USD",
        "require_usage": True,
        "require_cost": True,
        "timeout_seconds": 5.0,
        "max_response_bytes": 200_000,
        "max_input_characters": 100_000,
        "transport": httpx.MockTransport(handler),
    }
    options.update(overrides)
    return OpenAICompatibleEventBriefGenerator(**options)


def test_adapter_requests_strict_schema_and_returns_provider_usage_cost() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_provider_response(),
            headers={"x-request-id": "request-secret-456"},
            request=request,
        )

    adapter = _adapter(handler)
    result = asyncio.run(adapter(_model_input()))

    assert result.payload == _payload()
    assert result.usage.provider == "openrouter"
    assert result.usage.model == MODEL
    assert result.usage.input_tokens == 120
    assert result.usage.output_tokens == 30
    assert result.usage.billable_cost_micros == 175
    assert result.usage.currency == "USD"
    assert result.usage.provider_request_id == "request-secret-456"
    assert "provider-secret-key" not in repr(adapter)
    assert "request-secret-456" not in repr(result.usage)

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.headers["authorization"] == "Bearer provider-secret-key"
    body = json.loads(request.content)
    assert body["model"] == MODEL
    assert body["stream"] is False
    assert body["temperature"] == 0
    response_format = body["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["event_id"]["const"] == EVENT_ID
    assert schema["properties"]["source_ids"]["items"]["enum"] == [SOURCE_ID]
    assert "通期予想を10%修正した" in body["messages"][1]["content"]


def test_provider_generation_id_is_used_when_request_header_is_absent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_provider_response(), request=request)

    result = asyncio.run(_adapter(handler)(_model_input()))

    assert result.usage.provider_request_id == "gen-secret-123"


def test_provider_reported_model_is_preserved_for_usage_audit() -> None:
    actual_model = "openai/gpt-example-2026-07-01"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_provider_response(response_model=actual_model),
            request=request,
        )

    result = asyncio.run(_adapter(handler)(_model_input()))

    assert result.payload["model_version"] == MODEL
    assert result.usage.model == actual_model


def test_missing_cost_is_reported_unpriced_when_not_required() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_provider_response(include_cost=False),
            request=request,
        )

    result = asyncio.run(
        _adapter(handler, require_cost=False)(_model_input())
    )

    assert result.usage.input_tokens == 120
    assert result.usage.billable_cost_micros is None
    assert result.usage.currency is None
    assert result.usage.cost_status == "unpriced"


def test_missing_required_usage_fails_closed_with_sanitized_report() -> None:
    from src.market_morning.event_brief_service import EventBriefModelUnavailable

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_provider_response(include_usage=False),
            request=request,
        )

    with pytest.raises(EventBriefModelUnavailable) as error:
        asyncio.run(_adapter(handler)(_model_input()))

    assert str(error.value) == "event_brief_model_usage_missing"
    assert error.value.usage is not None
    assert error.value.usage.usage_status == "missing"
    assert "gen-secret-123" not in str(error.value)


def test_missing_required_cost_fails_closed_but_preserves_real_tokens() -> None:
    from src.market_morning.event_brief_service import EventBriefModelUnavailable

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_provider_response(include_cost=False),
            request=request,
        )

    with pytest.raises(EventBriefModelUnavailable) as error:
        asyncio.run(_adapter(handler)(_model_input()))

    assert str(error.value) == "event_brief_model_cost_missing"
    assert error.value.usage is not None
    assert error.value.usage.total_tokens == 150
    assert error.value.usage.cost_status == "unpriced"


@pytest.mark.parametrize("status_code", [400, 401, 403, 408, 429, 500, 503])
def test_http_failure_is_sanitized_and_never_persists_provider_body(
    status_code: int,
) -> None:
    from src.market_morning.event_brief_service import EventBriefModelUnavailable

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"error": {"message": "provider secret diagnostic"}},
            request=request,
        )

    with pytest.raises(EventBriefModelUnavailable) as error:
        asyncio.run(_adapter(handler)(_model_input()))

    assert str(error.value) == "event_brief_model_http_error"
    assert "diagnostic" not in str(error.value)
    assert error.value.usage is not None
    assert error.value.usage.usage_status == "missing"


def test_transport_failure_is_sanitized() -> None:
    from src.market_morning.event_brief_service import EventBriefModelUnavailable

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("proxy contains secret", request=request)

    with pytest.raises(EventBriefModelUnavailable) as error:
        asyncio.run(_adapter(handler)(_model_input()))

    assert str(error.value) == "event_brief_model_transport_unavailable"
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    ("content", "finish_reason", "error_code"),
    [
        ("not-json", "stop", "event_brief_model_response_invalid"),
        (json.dumps(_payload()), "length", "event_brief_model_incomplete"),
        (json.dumps([_payload()]), "stop", "event_brief_model_response_invalid"),
    ],
)
def test_invalid_model_content_preserves_usage_for_audit(
    content: str,
    finish_reason: str,
    error_code: str,
) -> None:
    from src.market_morning.event_brief_service import EventBriefModelUnavailable

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_provider_response(
                content=content,
                finish_reason=finish_reason,
            ),
            request=request,
        )

    with pytest.raises(EventBriefModelUnavailable) as error:
        asyncio.run(_adapter(handler)(_model_input()))

    assert str(error.value) == error_code
    assert error.value.usage is not None
    assert error.value.usage.total_tokens == 150
    assert error.value.usage.billable_cost_micros == 175


def test_oversized_provider_response_is_rejected_without_echoing_body() -> None:
    from src.market_morning.event_brief_service import EventBriefModelUnavailable

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"x" * 1001,
            headers={"content-length": "1001"},
            request=request,
        )

    with pytest.raises(EventBriefModelUnavailable) as error:
        asyncio.run(
            _adapter(handler, max_response_bytes=1000)(_model_input())
        )

    assert str(error.value) == "event_brief_model_response_too_large"
    assert "xxx" not in str(error.value)


def test_input_budget_is_enforced_before_provider_call() -> None:
    from src.market_morning.event_brief_service import EventBriefModelUnavailable

    def must_not_run(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("over-budget input must not call the provider")

    with pytest.raises(EventBriefModelUnavailable) as error:
        asyncio.run(
            _adapter(must_not_run, max_input_characters=100)(
                _model_input(evidence_text="証拠" * 100)
            )
        )

    assert str(error.value) == "event_brief_model_input_too_large"


@pytest.mark.parametrize(
    "endpoint_url",
    [
        "http://provider.example/v1/chat/completions",
        "https://user:secret@provider.example/v1/chat/completions",
        "https://provider.example/v1/chat/completions?debug=1",
        "not-a-url",
    ],
)
def test_adapter_configuration_rejects_unsafe_endpoint(endpoint_url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request)

    with pytest.raises(ValueError, match="endpoint_url"):
        _adapter(handler, endpoint_url=endpoint_url)


def _openrouter_adapter(handler, **overrides):
    from src.market_morning.event_brief_model_adapter import (
        OpenRouterEventBriefGenerator,
    )

    options = {
        "model": MODEL,
        "api_key": "openrouter-secret-key",
        "allowed_upstream_providers": ("openai", "azure/eastus"),
        "timeout_seconds": 5.0,
        "transport": httpx.MockTransport(handler),
    }
    options.update(overrides)
    return OpenRouterEventBriefGenerator(**options)


def test_openrouter_adapter_locks_routing_privacy_schema_and_billing() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_provider_response(),
            headers={"x-request-id": "openrouter-request-123"},
            request=request,
        )

    adapter = _openrouter_adapter(handler)
    result = asyncio.run(adapter(_model_input()))

    assert result.usage.provider == "openrouter"
    assert result.usage.input_tokens == 120
    assert result.usage.output_tokens == 30
    assert result.usage.billable_cost_micros == 175
    assert result.usage.currency == "USD"
    assert result.usage.provider_request_id == "openrouter-request-123"
    assert repr(adapter) == "<OpenRouterEventBriefGenerator configured>"
    assert "openrouter-secret-key" not in repr(adapter)

    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    body = json.loads(request.content)
    assert body["model"] == MODEL
    assert body["stream"] is False
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["provider"] == {
        "order": ["openai", "azure/eastus"],
        "only": ["openai", "azure/eastus"],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
    }


def test_openrouter_fallbacks_remain_limited_to_approved_providers() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_provider_response(), request=request)

    adapter = _openrouter_adapter(
        handler,
        allowed_upstream_providers=("anthropic", "amazon-bedrock"),
        allow_approved_fallbacks=True,
    )
    asyncio.run(adapter(_model_input()))

    policy = json.loads(requests[0].content)["provider"]
    assert policy["allow_fallbacks"] is True
    assert policy["only"] == ["anthropic", "amazon-bedrock"]
    assert policy["order"] == ["anthropic", "amazon-bedrock"]


@pytest.mark.parametrize(
    "endpoint_url",
    [
        "https://openrouter.ai/api/v1/chat/completions/",
        "https://openrouter.ai/api/v1/chat/completions?trace=1",
        "https://openrouter.example/api/v1/chat/completions",
        "http://openrouter.ai/api/v1/chat/completions",
    ],
)
def test_openrouter_adapter_rejects_unapproved_endpoint(endpoint_url: str) -> None:
    with pytest.raises(ValueError, match="approved OpenRouter endpoint"):
        _openrouter_adapter(lambda _request: httpx.Response(200), endpoint_url=endpoint_url)


@pytest.mark.parametrize(
    "providers",
    [
        (),
        ("openai", "OPENAI"),
        ("openai", "open ai"),
        ("openai", "https://provider.example"),
        tuple(f"provider-{index}" for index in range(11)),
    ],
)
def test_openrouter_adapter_rejects_unsafe_provider_allowlist(
    providers: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="provider"):
        _openrouter_adapter(
            lambda _request: httpx.Response(200),
            allowed_upstream_providers=providers,
        )


def test_openrouter_environment_factory_builds_policy_locked_adapter() -> None:
    from src.market_morning.event_brief_model_adapter import (
        build_openrouter_event_brief_generator_from_env,
    )

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_provider_response(), request=request)

    adapter = build_openrouter_event_brief_generator_from_env(
        {
            "VIBE_MARKET_MORNING_OPENROUTER_MODEL": MODEL,
            "VIBE_MARKET_MORNING_OPENROUTER_API_KEY": "factory-secret-key",
            "VIBE_MARKET_MORNING_OPENROUTER_UPSTREAM_PROVIDERS": (
                " OpenAI, Azure/EastUS "
            ),
            "VIBE_MARKET_MORNING_OPENROUTER_ENDPOINT": (
                "https://eu.openrouter.ai/api/v1/chat/completions"
            ),
            "VIBE_MARKET_MORNING_OPENROUTER_ALLOW_APPROVED_FALLBACKS": "TRUE",
        },
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(adapter(_model_input()))

    assert result.usage.billable_cost_micros == 175
    assert str(requests[0].url) == (
        "https://eu.openrouter.ai/api/v1/chat/completions"
    )
    assert json.loads(requests[0].content)["provider"] == {
        "order": ["openai", "azure/eastus"],
        "only": ["openai", "azure/eastus"],
        "allow_fallbacks": True,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
    }
    assert "factory-secret-key" not in repr(adapter)


@pytest.mark.parametrize("value", ["1", "yes", "on", "enabled", "", "tru"])
def test_openrouter_environment_factory_rejects_ambiguous_fallback_flag(
    value: str,
) -> None:
    from src.market_morning.event_brief_model_adapter import (
        build_openrouter_event_brief_generator_from_env,
    )

    with pytest.raises(ValueError, match="must be true or false"):
        build_openrouter_event_brief_generator_from_env(
            {
                "VIBE_MARKET_MORNING_OPENROUTER_MODEL": MODEL,
                "VIBE_MARKET_MORNING_OPENROUTER_API_KEY": "secret-key",
                "VIBE_MARKET_MORNING_OPENROUTER_UPSTREAM_PROVIDERS": "openai",
                "VIBE_MARKET_MORNING_OPENROUTER_ALLOW_APPROVED_FALLBACKS": value,
            }
        )


@pytest.mark.parametrize(
    "missing_key",
    [
        "VIBE_MARKET_MORNING_OPENROUTER_MODEL",
        "VIBE_MARKET_MORNING_OPENROUTER_API_KEY",
        "VIBE_MARKET_MORNING_OPENROUTER_UPSTREAM_PROVIDERS",
    ],
)
def test_openrouter_environment_factory_fails_closed_when_required_config_missing(
    missing_key: str,
) -> None:
    from src.market_morning.event_brief_model_adapter import (
        build_openrouter_event_brief_generator_from_env,
    )

    environment = {
        "VIBE_MARKET_MORNING_OPENROUTER_MODEL": MODEL,
        "VIBE_MARKET_MORNING_OPENROUTER_API_KEY": "secret-key",
        "VIBE_MARKET_MORNING_OPENROUTER_UPSTREAM_PROVIDERS": "openai",
    }
    del environment[missing_key]

    with pytest.raises(ValueError):
        build_openrouter_event_brief_generator_from_env(environment)
