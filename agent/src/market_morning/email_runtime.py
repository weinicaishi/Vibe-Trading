"""Strict production runner assembly for Market Morning reminder email.

The final deployment factory still owns provider approval and connectivity
preflight.  This module only turns already configured secrets and one verified
identity-directory adapter into the two durable worker ports, ensuring that
dispatch and delivery share the same opaque-token signer and database factory.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.market_morning.delivery_links import build_delivery_link_signer_from_env
from src.market_morning.email_delivery_service import (
    EmailDeliveryCommand,
    EmailDeliveryResult,
    run_email_delivery,
)
from src.market_morning.email_dispatch_service import (
    EmailDispatchCommand,
    EmailDispatchResult,
    dispatch_email_deliveries,
)
from src.market_morning.email_identity import (
    EMAIL_IDENTITY_FACTORY_ENV,
    build_email_identity_resolver,
)
from src.market_morning.jobs.handlers import EmailDeliveryRunner, EmailDispatchRunner
from src.market_morning.resend_email import build_resend_email_provider_from_env


class EmailRuntimeConfigurationError(RuntimeError):
    """Stable runner-assembly failure without secret or provider payload."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


@dataclass(frozen=True, slots=True, repr=False)
class ResendEmailRuntimeRunners:
    """The paired worker ports required by ``assemble_runtime_dependencies``."""

    email_delivery_runner: EmailDeliveryRunner = field(repr=False)
    email_dispatch_runner: EmailDispatchRunner = field(repr=False)
    provider: str = "resend"

    def __post_init__(self) -> None:
        if (
            self.provider != "resend"
            or not callable(self.email_delivery_runner)
            or not callable(self.email_dispatch_runner)
        ):
            raise EmailRuntimeConfigurationError("email_runtime_contract_invalid")

    def __repr__(self) -> str:
        return "<ResendEmailRuntimeRunners configured>"


def build_resend_email_runtime_runners(
    *,
    session_factory: async_sessionmaker[AsyncSession] | Any,
    environ: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> ResendEmailRuntimeRunners:
    """Build paired Resend dispatch/delivery ports or fail before worker start.

    Construction validates all local configuration without making a provider
    request.  A deployment must still supply the runtime ``email_delivery``
    preflight that proves its approved account/domain is usable without sending
    a message.
    """

    if not callable(getattr(session_factory, "begin", None)):
        raise EmailRuntimeConfigurationError(
            "email_runtime_session_factory_invalid"
        )
    if not callable(clock):
        raise EmailRuntimeConfigurationError("email_runtime_clock_invalid")
    values = os.environ if environ is None else environ
    if not isinstance(values, Mapping):
        raise EmailRuntimeConfigurationError("email_runtime_environment_invalid")

    identity_resolver = build_email_identity_resolver(
        values.get(EMAIL_IDENTITY_FACTORY_ENV, "")
    )
    signer = build_delivery_link_signer_from_env(values)
    provider = build_resend_email_provider_from_env(values, transport=transport)

    async def delivery_runner(
        command: EmailDeliveryCommand,
    ) -> EmailDeliveryResult:
        return await run_email_delivery(
            command,
            identity_resolver=identity_resolver,
            link_builder=signer.link_builder,
            provider=provider,
            session_factory=session_factory,
            clock=clock,
        )

    async def dispatch_runner(
        command: EmailDispatchCommand,
    ) -> EmailDispatchResult:
        return await dispatch_email_deliveries(
            command,
            token_digest_builder=signer.token_digest_builder,
            session_factory=session_factory,
        )

    return ResendEmailRuntimeRunners(
        email_delivery_runner=delivery_runner,
        email_dispatch_runner=dispatch_runner,
    )


__all__ = [
    "EmailRuntimeConfigurationError",
    "ResendEmailRuntimeRunners",
    "build_resend_email_runtime_runners",
]
