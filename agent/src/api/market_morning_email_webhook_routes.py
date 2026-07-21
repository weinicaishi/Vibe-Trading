"""Fail-closed HTTP boundary for email-provider delivery callbacks."""

from __future__ import annotations

import importlib
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Path, Request, status
from pydantic import BaseModel

from src.config.accessor import get_env_config
from src.market_morning.email_webhook_service import (
    DeliveryWebhookEventParser,
    DeliveryWebhookSignatureVerifier,
    EmailWebhookRejected,
    MAX_WEBHOOK_BODY_BYTES,
    process_email_delivery_webhook,
)

logger = logging.getLogger(__name__)

_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class EmailWebhookConfigurationError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


def _provider(value: str) -> str:
    normalized = value.strip()
    if not _PROVIDER_RE.fullmatch(normalized):
        raise EmailWebhookConfigurationError("email_webhook_provider_invalid")
    return normalized


@dataclass(frozen=True, slots=True)
class EmailWebhookAdapter:
    """Deployment-owned provider verification and normalization ports."""

    provider: str
    signature_verifier: DeliveryWebhookSignatureVerifier
    event_parser: DeliveryWebhookEventParser
    session_factory: Any | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _provider(self.provider))
        if not callable(self.signature_verifier) or not callable(self.event_parser):
            raise EmailWebhookConfigurationError(
                "email_webhook_factory_contract_invalid"
            )


EmailWebhookAdapterResolver = Callable[[str], EmailWebhookAdapter | None]


class EmailWebhookAcknowledgement(BaseModel):
    status: Literal["accepted", "duplicate"]


def load_email_webhook_adapters(
    factory_path: str,
) -> dict[str, EmailWebhookAdapter]:
    """Load a deployment-owned ``module:function`` adapter factory."""

    normalized = factory_path.strip()
    if not normalized or normalized.count(":") != 1:
        raise EmailWebhookConfigurationError("email_webhook_factory_invalid")
    module_name, attribute_name = normalized.split(":", 1)
    if not module_name or not attribute_name:
        raise EmailWebhookConfigurationError("email_webhook_factory_invalid")

    try:
        factory = getattr(importlib.import_module(module_name), attribute_name)
        adapters = factory()
    except Exception as error:
        logger.error(
            "Market Morning email webhook factory failed: exception_type=%s",
            type(error).__name__,
        )
        raise EmailWebhookConfigurationError(
            "email_webhook_factory_failed"
        ) from None

    if not isinstance(adapters, Mapping) or not adapters:
        raise EmailWebhookConfigurationError(
            "email_webhook_factory_contract_invalid"
        )

    validated: dict[str, EmailWebhookAdapter] = {}
    try:
        for raw_provider, adapter in adapters.items():
            provider = _provider(raw_provider)
            if not isinstance(adapter, EmailWebhookAdapter):
                raise TypeError("adapter contract is invalid")
            if adapter.provider != provider or provider in validated:
                raise ValueError("adapter provider identity is invalid")
            validated[provider] = adapter
    except Exception as error:
        if isinstance(error, EmailWebhookConfigurationError):
            raise
        raise EmailWebhookConfigurationError(
            "email_webhook_factory_contract_invalid"
        ) from None
    return validated


@lru_cache(maxsize=8)
def _load_configured_adapters(
    factory_path: str,
) -> dict[str, EmailWebhookAdapter]:
    return load_email_webhook_adapters(factory_path)


def clear_configured_adapter_cache() -> None:
    _load_configured_adapters.cache_clear()


def configured_adapter_cache_info():
    return _load_configured_adapters.cache_info()


def _resolve_configured_adapter(provider: str) -> EmailWebhookAdapter | None:
    factory_path = (
        get_env_config().market_morning.email_webhook_factory.strip()
    )
    if not factory_path:
        raise EmailWebhookConfigurationError(
            "email_webhook_factory_not_configured"
        )
    return _load_configured_adapters(factory_path).get(provider)


async def _read_bounded_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_WEBHOOK_BODY_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="Email delivery webhook body is too large",
                )
        except ValueError:
            pass

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_WEBHOOK_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Email delivery webhook body is too large",
            )
        chunks.append(chunk)
    raw_body = b"".join(chunks)
    if not raw_body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email delivery webhook payload is invalid",
        )
    return raw_body


def _raise_processing_http_error(error: Exception) -> None:
    if isinstance(error, EmailWebhookRejected):
        if error.error_code == "delivery_webhook_signature_invalid":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Email delivery webhook authentication failed",
            ) from error
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email delivery webhook payload is invalid",
        ) from error

    logger.warning(
        "Market Morning email webhook failed: exception_type=%s",
        type(error).__name__,
    )
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Email delivery webhook is temporarily unavailable",
    ) from error


def register_market_morning_email_webhook_routes(
    app: FastAPI,
    *,
    adapter_resolver: EmailWebhookAdapterResolver | None = None,
) -> None:
    """Register the provider-authenticated callback outside product auth."""

    resolve_adapter = adapter_resolver or _resolve_configured_adapter

    @app.post(
        "/market-morning/webhooks/email/{provider}",
        response_model=EmailWebhookAcknowledgement,
    )
    async def market_morning_email_delivery_webhook(
        request: Request,
        provider: str = Path(
            min_length=1,
            max_length=64,
            pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$",
        ),
    ) -> EmailWebhookAcknowledgement:
        if not get_env_config().market_morning.enabled:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Market Morning is disabled",
            )

        try:
            adapter = resolve_adapter(provider)
            if adapter is not None and not isinstance(adapter, EmailWebhookAdapter):
                raise EmailWebhookConfigurationError(
                    "email_webhook_factory_contract_invalid"
                )
        except Exception as error:
            logger.warning(
                "Market Morning email webhook adapter unavailable: "
                "exception_type=%s",
                type(error).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Email delivery webhook is temporarily unavailable",
            ) from error
        if adapter is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Email delivery webhook provider not found",
            )

        raw_body = await _read_bounded_body(request)
        try:
            result = await process_email_delivery_webhook(
                provider=provider,
                raw_body=raw_body,
                headers=dict(request.headers.items()),
                signature_verifier=adapter.signature_verifier,
                event_parser=adapter.event_parser,
                session_factory=adapter.session_factory,
            )
        except Exception as error:
            _raise_processing_http_error(error)
        return EmailWebhookAcknowledgement(
            status="duplicate" if result.duplicate else "accepted"
        )


__all__ = [
    "EmailWebhookAcknowledgement",
    "EmailWebhookAdapter",
    "EmailWebhookAdapterResolver",
    "EmailWebhookConfigurationError",
    "clear_configured_adapter_cache",
    "configured_adapter_cache_info",
    "load_email_webhook_adapters",
    "register_market_morning_email_webhook_routes",
]
