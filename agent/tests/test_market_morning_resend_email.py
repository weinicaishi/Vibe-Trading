"""Concrete Resend delivery and signed-webhook adapter contracts."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

NOW = datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)
SECRET_BYTES = b"market-morning-resend-secret!"[:24]
WEBHOOK_SECRET = "whsec_" + base64.b64encode(SECRET_BYTES).decode("ascii")
PREVIOUS_SECRET_BYTES = b"previous-market-morning!!"[:24]
PREVIOUS_WEBHOOK_SECRET = "whsec_" + base64.b64encode(
    PREVIOUS_SECRET_BYTES
).decode("ascii")
MESSAGE_ID = "provider-message-1"
EVENT_ID = "msg_event_1"


def _message():
    from src.market_morning.email_delivery_service import EmailReminderMessage

    return EmailReminderMessage(
        destination="private@example.jp",
        private_url="https://app.example.jp/market-morning/open/private-token",
        idempotency_key="market-morning/2026-07-21/user-1",
    )


def _webhook_payload(event_type: str = "email.delivered") -> bytes:
    return json.dumps(
        {
            "type": event_type,
            "created_at": "2026-07-21T00:00:00Z",
            "data": {
                "email_id": MESSAGE_ID,
                "to": ["private@example.jp"],
                "subject": "private subject that must not be persisted",
            },
        },
        separators=(",", ":"),
    ).encode()


def _signature(
    body: bytes,
    *,
    timestamp: int | None = None,
    secret: bytes = SECRET_BYTES,
    event_id: str = EVENT_ID,
) -> tuple[str, dict[str, str]]:
    canonical_timestamp = timestamp or int(NOW.timestamp())
    signed = f"{event_id}.{canonical_timestamp}.".encode("ascii") + body
    signature = base64.b64encode(
        hmac.new(secret, signed, hashlib.sha256).digest()
    ).decode("ascii")
    return signature, {
        "svix-id": event_id,
        "svix-timestamp": str(canonical_timestamp),
        "svix-signature": f"v1,{signature}",
    }


def _provider(handler, **overrides):
    from src.market_morning.resend_email import ResendEmailProvider

    options = {
        "api_key": "re_private_provider_key",
        "from_address": "Market Morning <morning@example.jp>",
        "timeout_seconds": 5,
        "max_response_bytes": 64_000,
        "transport": httpx.MockTransport(handler),
    }
    options.update(overrides)
    return ResendEmailProvider(**options)


def test_sender_uses_fixed_endpoint_template_and_provider_idempotency() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": MESSAGE_ID}, request=request)

    provider = _provider(handler)
    result = asyncio.run(provider(_message()))

    assert result.provider_message_id == MESSAGE_ID
    assert repr(provider) == "<ResendEmailProvider configured>"
    assert "re_private_provider_key" not in repr(provider)
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://api.resend.com/emails"
    assert request.method == "POST"
    assert request.headers["authorization"] == "Bearer re_private_provider_key"
    assert request.headers["idempotency-key"] == _message().idempotency_key
    payload = json.loads(request.content)
    assert payload == {
        "from": "Market Morning <morning@example.jp>",
        "to": ["private@example.jp"],
        "subject": _message().subject,
        "text": f"{_message().body}\n\n{_message().private_url}",
    }
    assert "tags" not in payload


@pytest.mark.parametrize("status_code", [301, 400, 401, 409, 422, 429, 500, 503])
def test_sender_sanitizes_all_provider_http_failures(status_code: int) -> None:
    from src.market_morning.email_delivery_service import (
        EmailDeliveryPortUnavailable,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"message": "provider credential and recipient diagnostic"},
            request=request,
        )

    with pytest.raises(EmailDeliveryPortUnavailable) as error:
        asyncio.run(_provider(handler)(_message()))

    assert str(error.value) == "email_provider_unavailable"
    assert "credential" not in str(error.value)
    assert "recipient" not in str(error.value)


def test_sender_sanitizes_transport_and_invalid_receipt_failures() -> None:
    from src.market_morning.email_delivery_service import (
        EmailDeliveryPortUnavailable,
    )

    def transport_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("proxy secret", request=request)

    with pytest.raises(EmailDeliveryPortUnavailable) as transport_error:
        asyncio.run(_provider(transport_failure)(_message()))
    assert str(transport_error.value) == "email_provider_unavailable"
    assert "secret" not in str(transport_error.value)

    def invalid_receipt(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": "x" * 192, "private": "must-not-leak"},
            request=request,
        )

    with pytest.raises(EmailDeliveryPortUnavailable) as receipt_error:
        asyncio.run(_provider(invalid_receipt)(_message()))
    assert str(receipt_error.value) == "email_provider_unavailable"
    assert "must-not-leak" not in str(receipt_error.value)


def test_sender_rejects_oversized_response() -> None:
    from src.market_morning.email_delivery_service import (
        EmailDeliveryPortUnavailable,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"x" * 1_001,
            headers={"content-length": "1001"},
            request=request,
        )

    with pytest.raises(EmailDeliveryPortUnavailable):
        asyncio.run(_provider(handler, max_response_bytes=1_000)(_message()))


@pytest.mark.parametrize(
    ("api_key", "from_address", "error_code"),
    [
        ("", "morning@example.jp", "resend_api_key_invalid"),
        ("re_key", "", "resend_sender_invalid"),
        ("re_key", "not-an-email", "resend_sender_invalid"),
        ("re_key", "morning@example.jp\nBcc: hidden@example.jp", "resend_sender_invalid"),
    ],
)
def test_sender_configuration_fails_closed(
    api_key: str,
    from_address: str,
    error_code: str,
) -> None:
    from src.market_morning.resend_email import (
        ResendEmailConfigurationError,
        ResendEmailProvider,
    )

    with pytest.raises(ResendEmailConfigurationError) as error:
        ResendEmailProvider(api_key=api_key, from_address=from_address)
    assert error.value.error_code == error_code
    assert api_key not in str(error.value) or not api_key


def test_signature_verifier_accepts_exact_raw_body_and_rotating_secret() -> None:
    from src.market_morning.resend_email import ResendWebhookSignatureVerifier

    body = _webhook_payload()
    current_signature, headers = _signature(body)
    previous_signature, _ = _signature(body, secret=PREVIOUS_SECRET_BYTES)
    headers["Svix-Signature"] = (
        f"v1,invalid v1,{previous_signature} v1,{current_signature}"
    )
    headers.pop("svix-signature")
    verifier = ResendWebhookSignatureVerifier(
        webhook_secret=WEBHOOK_SECRET,
        previous_webhook_secret=PREVIOUS_WEBHOOK_SECRET,
        clock=lambda: NOW,
    )

    assert asyncio.run(verifier(body, headers)) is True
    assert repr(verifier) == "<ResendWebhookSignatureVerifier configured>"
    assert WEBHOOK_SECRET not in repr(verifier)


@pytest.mark.parametrize(
    "mutation",
    ["body", "signature", "missing_id", "dotted_id", "stale", "future"],
)
def test_signature_verifier_rejects_tampering_and_replay(mutation: str) -> None:
    from src.market_morning.resend_email import ResendWebhookSignatureVerifier

    body = _webhook_payload()
    _, headers = _signature(body)
    candidate_body = body
    if mutation == "body":
        candidate_body += b" "
    elif mutation == "signature":
        headers["svix-signature"] = "v1,invalid"
    elif mutation == "missing_id":
        headers.pop("svix-id")
    elif mutation == "dotted_id":
        _, headers = _signature(body, event_id="event.invalid")
    elif mutation == "stale":
        _, headers = _signature(
            body,
            timestamp=int((NOW - timedelta(seconds=301)).timestamp()),
        )
    elif mutation == "future":
        _, headers = _signature(
            body,
            timestamp=int((NOW + timedelta(seconds=301)).timestamp()),
        )
    verifier = ResendWebhookSignatureVerifier(
        webhook_secret=WEBHOOK_SECRET,
        clock=lambda: NOW,
    )

    assert asyncio.run(verifier(candidate_body, headers)) is False


@pytest.mark.parametrize(
    ("provider_type", "normalized_type"),
    [
        ("email.delivered", "delivered"),
        ("email.clicked", "clicked"),
        ("email.bounced", "failed"),
        ("email.complained", "failed"),
        ("email.failed", "failed"),
        ("email.suppressed", "failed"),
    ],
)
def test_webhook_parser_uses_signed_event_id_and_minimal_delivery_fields(
    provider_type: str,
    normalized_type: str,
) -> None:
    from src.market_morning.resend_email import ResendWebhookEventParser

    result = asyncio.run(
        ResendWebhookEventParser()(
            _webhook_payload(provider_type),
            {"svix-id": EVENT_ID},
        )
    )

    assert result.provider == "resend"
    assert result.provider_event_id == EVENT_ID
    assert result.provider_message_id == MESSAGE_ID
    assert result.event_type.value == normalized_type
    assert "private@example.jp" not in repr(result)
    assert "private subject" not in repr(result)


@pytest.mark.parametrize(
    ("body", "headers"),
    [
        (b"not-json", {"svix-id": EVENT_ID}),
        (_webhook_payload("email.opened"), {"svix-id": EVENT_ID}),
        (_webhook_payload(), {}),
        (b'{"type":"email.delivered","data":{}}', {"svix-id": EVENT_ID}),
    ],
)
def test_webhook_parser_rejects_unsubscribed_or_incomplete_events(
    body: bytes,
    headers: dict[str, str],
) -> None:
    from src.market_morning.resend_email import ResendWebhookEventParser

    with pytest.raises(ValueError, match="invalid"):
        asyncio.run(ResendWebhookEventParser()(body, headers))


def test_environment_builders_keep_secrets_out_of_shared_config() -> None:
    from src.api.market_morning_resend_webhook_factory import (
        build_resend_webhook_adapters,
    )
    from src.market_morning.resend_email import (
        ResendEmailProvider,
        build_resend_email_provider_from_env,
    )

    environment = {
        "VIBE_MARKET_MORNING_RESEND_API_KEY": "re_private_provider_key",
        "VIBE_MARKET_MORNING_RESEND_FROM": "morning@example.jp",
        "VIBE_MARKET_MORNING_RESEND_WEBHOOK_SECRET": WEBHOOK_SECRET,
        "VIBE_MARKET_MORNING_RESEND_WEBHOOK_SECRET_PREVIOUS": PREVIOUS_WEBHOOK_SECRET,
    }
    provider = build_resend_email_provider_from_env(environment)
    adapters = build_resend_webhook_adapters(environment)

    assert isinstance(provider, ResendEmailProvider)
    assert set(adapters) == {"resend"}
    assert adapters["resend"].provider == "resend"
    combined_repr = f"{provider!r} {adapters!r}"
    assert "re_private_provider_key" not in combined_repr
    assert WEBHOOK_SECRET not in combined_repr
    assert PREVIOUS_WEBHOOK_SECRET not in combined_repr


def test_invalid_webhook_secret_is_rejected_without_echoing_value() -> None:
    from src.market_morning.resend_email import (
        ResendEmailConfigurationError,
        ResendWebhookSignatureVerifier,
    )

    invalid = "whsec_not-base64-private-value"
    with pytest.raises(ResendEmailConfigurationError) as error:
        ResendWebhookSignatureVerifier(webhook_secret=invalid)

    assert error.value.error_code == "resend_webhook_secret_invalid"
    assert invalid not in str(error.value)
