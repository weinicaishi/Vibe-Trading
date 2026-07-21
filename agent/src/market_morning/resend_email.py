"""Concrete Resend adapters for Market Morning reminder delivery.

The adapter keeps provider credentials and raw webhook payloads outside the
domain model.  Sending uses Resend's HTTPS API with an application-owned
idempotency key.  Webhooks are verified against the exact raw body and Svix
metadata before a deliberately small delivery event is normalized.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from email.utils import parseaddr

import httpx

from src.market_morning.email_delivery_service import (
    EmailDeliveryPortUnavailable,
    EmailProviderReceipt,
    EmailReminderMessage,
)
from src.market_morning.email_webhook_service import (
    NormalizedDeliveryProviderEvent,
)
from src.market_morning.repositories.delivery_webhooks import (
    DeliveryProviderEventType,
)

RESEND_API_URL = "https://api.resend.com/emails"
RESEND_PROVIDER = "resend"

_DEFAULT_RESPONSE_LIMIT = 64 * 1024
_DEFAULT_WEBHOOK_TOLERANCE_SECONDS = 300
_MAX_SIGNATURE_HEADER_LENGTH = 4_096

_EVENT_TYPE_MAP = {
    "email.delivered": DeliveryProviderEventType.DELIVERED,
    "email.clicked": DeliveryProviderEventType.CLICKED,
    "email.bounced": DeliveryProviderEventType.FAILED,
    "email.complained": DeliveryProviderEventType.FAILED,
    "email.failed": DeliveryProviderEventType.FAILED,
    "email.suppressed": DeliveryProviderEventType.FAILED,
}


class ResendEmailConfigurationError(ValueError):
    """Stable configuration failure that never includes a credential."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


def _bounded_secret(value: str, *, error_code: str) -> str:
    if not isinstance(value, str):
        raise ResendEmailConfigurationError(error_code)
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 4_096
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise ResendEmailConfigurationError(error_code)
    return normalized


def _sender(value: str) -> str:
    if not isinstance(value, str):
        raise ResendEmailConfigurationError("resend_sender_invalid")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 320
        or "\r" in normalized
        or "\n" in normalized
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise ResendEmailConfigurationError("resend_sender_invalid")
    _, address = parseaddr(normalized)
    if (
        not address
        or "@" not in address
        or address.startswith("@")
        or address.endswith("@")
    ):
        raise ResendEmailConfigurationError("resend_sender_invalid")
    return normalized


def _positive_int(
    value: int,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}")
    return value


def _header_map(headers: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(headers, Mapping):
        return {}
    normalized: dict[str, str] = {}
    for key, value in headers.items():
        if isinstance(key, str) and isinstance(value, str):
            normalized[key.lower()] = value
    return normalized


async def _bounded_response_body(
    response: httpx.Response,
    *,
    maximum: int,
) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > maximum:
                raise EmailDeliveryPortUnavailable("email_provider_unavailable")
        except ValueError:
            pass
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > maximum:
            raise EmailDeliveryPortUnavailable("email_provider_unavailable")
        chunks.append(chunk)
    return b"".join(chunks)


class ResendEmailProvider:
    """Send the fixed Market Morning reminder through Resend's Email API."""

    def __init__(
        self,
        *,
        api_key: str,
        from_address: str,
        timeout_seconds: float = 10.0,
        max_response_bytes: int = _DEFAULT_RESPONSE_LIMIT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = _bounded_secret(
            api_key,
            error_code="resend_api_key_invalid",
        )
        self._from_address = _sender(from_address)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0.1 <= float(timeout_seconds) <= 60.0
        ):
            raise ValueError("timeout_seconds must be between 0.1 and 60")
        self._timeout = httpx.Timeout(float(timeout_seconds))
        self._max_response_bytes = _positive_int(
            max_response_bytes,
            field_name="max_response_bytes",
            minimum=1_000,
            maximum=1_000_000,
        )
        self._transport = transport

    def __repr__(self) -> str:
        return "<ResendEmailProvider configured>"

    async def __call__(
        self,
        message: EmailReminderMessage,
    ) -> EmailProviderReceipt:
        if not isinstance(message, EmailReminderMessage):
            raise TypeError("message must be an EmailReminderMessage")
        request_body = {
            "from": self._from_address,
            "to": [message.destination],
            "subject": message.subject,
            "text": f"{message.body}\n\n{message.private_url}",
        }
        headers = {
            "authorization": f"Bearer {self._api_key}",
            "accept": "application/json",
            "content-type": "application/json",
            "idempotency-key": message.idempotency_key,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                async with client.stream(
                    "POST",
                    RESEND_API_URL,
                    headers=headers,
                    json=request_body,
                ) as response:
                    if response.status_code not in {200, 201}:
                        raise EmailDeliveryPortUnavailable(
                            "email_provider_unavailable"
                        )
                    raw_response = await _bounded_response_body(
                        response,
                        maximum=self._max_response_bytes,
                    )
        except EmailDeliveryPortUnavailable:
            raise
        except httpx.HTTPError:
            raise EmailDeliveryPortUnavailable(
                "email_provider_unavailable"
            ) from None

        try:
            payload = json.loads(raw_response)
            provider_message_id = payload["id"]
            if (
                not isinstance(payload, dict)
                or not isinstance(provider_message_id, str)
                or not provider_message_id.strip()
                or len(provider_message_id.strip()) > 191
            ):
                raise ValueError("invalid provider receipt")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            raise EmailDeliveryPortUnavailable(
                "email_provider_unavailable"
            ) from None
        return EmailProviderReceipt(provider_message_id=provider_message_id)


def _decode_webhook_secret(secret: str) -> bytes:
    normalized = _bounded_secret(
        secret,
        error_code="resend_webhook_secret_invalid",
    )
    if not normalized.startswith("whsec_"):
        raise ResendEmailConfigurationError("resend_webhook_secret_invalid")
    try:
        decoded = base64.b64decode(normalized[6:], validate=True)
    except (binascii.Error, ValueError):
        raise ResendEmailConfigurationError(
            "resend_webhook_secret_invalid"
        ) from None
    if not 24 <= len(decoded) <= 64:
        raise ResendEmailConfigurationError("resend_webhook_secret_invalid")
    return decoded


class ResendWebhookSignatureVerifier:
    """Verify Resend/Svix HMAC signatures with replay-window enforcement."""

    def __init__(
        self,
        *,
        webhook_secret: str,
        previous_webhook_secret: str | None = None,
        tolerance_seconds: int = _DEFAULT_WEBHOOK_TOLERANCE_SECONDS,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        secrets = [_decode_webhook_secret(webhook_secret)]
        if previous_webhook_secret:
            secrets.append(_decode_webhook_secret(previous_webhook_secret))
        self._secrets = tuple(secrets)
        self._tolerance_seconds = _positive_int(
            tolerance_seconds,
            field_name="tolerance_seconds",
            minimum=30,
            maximum=900,
        )
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock

    def __repr__(self) -> str:
        return "<ResendWebhookSignatureVerifier configured>"

    async def __call__(
        self,
        raw_body: bytes,
        headers: Mapping[str, str],
    ) -> bool:
        if not isinstance(raw_body, bytes) or not raw_body:
            return False
        canonical = _header_map(headers)
        message_id = canonical.get("svix-id", "")
        timestamp = canonical.get("svix-timestamp", "")
        signatures = canonical.get("svix-signature", "")
        if (
            not message_id
            or len(message_id) > 191
            or "." in message_id
            or any(ord(character) < 33 or ord(character) == 127 for character in message_id)
            or not timestamp.isascii()
            or not timestamp.isdecimal()
            or len(timestamp) > 20
            or not signatures
            or len(signatures) > _MAX_SIGNATURE_HEADER_LENGTH
        ):
            return False
        try:
            attempt_timestamp = int(timestamp)
            current = self._clock()
            if current.tzinfo is None or current.utcoffset() is None:
                return False
            if abs(int(current.timestamp()) - attempt_timestamp) > self._tolerance_seconds:
                return False
            signed_payload = (
                f"{message_id}.{timestamp}.".encode("ascii") + raw_body
            )
        except (OverflowError, ValueError):
            return False

        candidates = tuple(
            token[3:]
            for token in signatures.split()
            if token.startswith("v1,") and len(token) > 3
        )
        if not candidates:
            return False
        for secret in self._secrets:
            expected = base64.b64encode(
                hmac.new(secret, signed_payload, hashlib.sha256).digest()
            ).decode("ascii")
            if any(hmac.compare_digest(expected, candidate) for candidate in candidates):
                return True
        return False


class ResendWebhookEventParser:
    """Normalize only the delivery lifecycle events used by Market Morning."""

    async def __call__(
        self,
        raw_body: bytes,
        headers: Mapping[str, str],
    ) -> NormalizedDeliveryProviderEvent:
        canonical = _header_map(headers)
        provider_event_id = canonical.get("svix-id", "")
        try:
            payload = json.loads(
                raw_body,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    ValueError("invalid JSON constant")
                ),
            )
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            event_type = _EVENT_TYPE_MAP[payload["type"]]
            data = payload["data"]
            if not isinstance(data, dict):
                raise ValueError("data must be an object")
            provider_message_id = data["email_id"]
        except (
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ):
            raise ValueError("resend webhook payload is invalid") from None
        try:
            return NormalizedDeliveryProviderEvent(
                provider=RESEND_PROVIDER,
                provider_event_id=provider_event_id,
                provider_message_id=provider_message_id,
                event_type=event_type,
            )
        except (TypeError, ValueError):
            raise ValueError("resend webhook payload is invalid") from None


def build_resend_email_provider_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ResendEmailProvider:
    """Build the sending port without copying secrets into shared config."""

    values = os.environ if environ is None else environ
    return ResendEmailProvider(
        api_key=values.get("VIBE_MARKET_MORNING_RESEND_API_KEY", ""),
        from_address=values.get("VIBE_MARKET_MORNING_RESEND_FROM", ""),
        transport=transport,
    )


__all__ = [
    "RESEND_API_URL",
    "RESEND_PROVIDER",
    "ResendEmailConfigurationError",
    "ResendEmailProvider",
    "ResendWebhookEventParser",
    "ResendWebhookSignatureVerifier",
    "build_resend_email_provider_from_env",
]
