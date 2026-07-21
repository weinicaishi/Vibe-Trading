"""Environment-backed Resend webhook factory for the Market Morning API."""

from __future__ import annotations

import os
from collections.abc import Mapping

from src.api.market_morning_email_webhook_routes import EmailWebhookAdapter
from src.market_morning.resend_email import (
    RESEND_PROVIDER,
    ResendWebhookEventParser,
    ResendWebhookSignatureVerifier,
)


def build_resend_webhook_adapters(
    environ: Mapping[str, str] | None = None,
) -> dict[str, EmailWebhookAdapter]:
    """Build the verified Resend adapter expected by the generic route."""

    values = os.environ if environ is None else environ
    current = values.get("VIBE_MARKET_MORNING_RESEND_WEBHOOK_SECRET", "")
    previous = values.get(
        "VIBE_MARKET_MORNING_RESEND_WEBHOOK_SECRET_PREVIOUS",
        "",
    ).strip()
    adapter = EmailWebhookAdapter(
        provider=RESEND_PROVIDER,
        signature_verifier=ResendWebhookSignatureVerifier(
            webhook_secret=current,
            previous_webhook_secret=previous or None,
        ),
        event_parser=ResendWebhookEventParser(),
    )
    return {RESEND_PROVIDER: adapter}


__all__ = ["build_resend_webhook_adapters"]
