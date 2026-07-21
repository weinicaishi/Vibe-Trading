"""Opaque, session-bound email deep links without plaintext identity in URLs."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
import os
import re
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.market_morning.db import get_session_factory
from src.market_morning.email_delivery_service import (
    DeliveryDeepLink,
    EmailDeliveryContext,
    EmailDeliveryPortUnavailable,
)
from src.market_morning.http_reachability import normalize_source_hostname
from src.market_morning.models import DeliveryAttemptRecord

DELIVERY_LINK_BASE_URL_ENV = "VIBE_MARKET_MORNING_DELIVERY_LINK_BASE_URL"
DELIVERY_LINK_SECRET_ENV = "VIBE_MARKET_MORNING_DELIVERY_LINK_SIGNING_SECRET"
DELIVERY_LINK_PREVIOUS_SECRET_ENV = (
    "VIBE_MARKET_MORNING_DELIVERY_LINK_SIGNING_SECRET_PREVIOUS"
)
DELIVERY_LINK_DESTINATION_PATH = "/market-morning"
DELIVERY_LINK_MAX_AGE = timedelta(days=7)
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")


class DeliveryLinkConfigurationError(ValueError):
    """Safe configuration error that never includes a key or URL."""


class DeliveryLinkUnavailable(RuntimeError):
    """The token is invalid, expired, wrong-user or otherwise not redeemable."""


@dataclass(frozen=True, slots=True)
class DeliveryLinkRedemption:
    edition_date: date
    destination_path: str = DELIVERY_LINK_DESTINATION_PATH

    def __post_init__(self) -> None:
        if type(self.edition_date) is not date:
            raise ValueError("edition_date must be a date")
        if self.destination_path != DELIVERY_LINK_DESTINATION_PATH:
            raise ValueError("delivery link destination is invalid")


def _uuid(value: str, *, field_name: str) -> str:
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise ValueError(f"{field_name} must be a UUID") from None


def _mysql_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _canonical_token(value: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise ValueError("delivery token is invalid")
    return value


def _token_digest(token: str) -> str:
    return hashlib.sha256(_canonical_token(token).encode("ascii")).hexdigest()


def _secret(value: bytes) -> bytes:
    if not isinstance(value, bytes) or not 32 <= len(value) <= 512:
        raise DeliveryLinkConfigurationError("delivery_link_secret_invalid")
    return bytes(value)


def _base_url(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2_048:
        raise DeliveryLinkConfigurationError("delivery_link_base_url_invalid")
    try:
        parsed = urlsplit(value)
        host = normalize_source_hostname(parsed.hostname or "")
        port = parsed.port
    except (TypeError, UnicodeError, ValueError):
        raise DeliveryLinkConfigurationError("delivery_link_base_url_invalid") from None
    try:
        decoded_path = parsed.path
        for _ in range(4):
            next_path = unquote(decoded_path, errors="strict")
            if next_path == decoded_path:
                break
            decoded_path = next_path
    except (UnicodeDecodeError, ValueError):
        raise DeliveryLinkConfigurationError("delivery_link_base_url_invalid") from None
    path_segments = decoded_path.replace("\\", "/").split("/")
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or any(ord(character) < 32 for character in parsed.path)
        or "%" in decoded_path
        or any(ord(character) < 32 for character in decoded_path)
        or any(segment in (".", "..") for segment in path_segments)
    ):
        raise DeliveryLinkConfigurationError("delivery_link_base_url_invalid")
    return urlunsplit(("https", host, parsed.path or "/", "", ""))


class DeliveryLinkSigner:
    """Deterministically rebuild a token at dispatch and delivery time.

    Only the SHA-256 of the token is stored.  The URL contains no user, run or
    edition identifier, and the token is placed in the fragment so it is not
    sent in the initial HTTP request or ordinary access log.  A previous key is
    accepted only to finish deliveries dispatched immediately before rotation.
    """

    def __init__(
        self,
        *,
        base_url: str,
        signing_secret: bytes,
        previous_signing_secret: bytes | None = None,
    ) -> None:
        self._base_url = _base_url(base_url)
        primary = _secret(signing_secret)
        previous = (
            None
            if previous_signing_secret is None
            else _secret(previous_signing_secret)
        )
        if previous is not None and hmac.compare_digest(primary, previous):
            raise DeliveryLinkConfigurationError("delivery_link_secret_invalid")
        self._secrets = (primary,) if previous is None else (primary, previous)

    def __repr__(self) -> str:
        return "<DeliveryLinkSigner configured>"

    @staticmethod
    def _identity(user_id: str, global_run_id: str, edition_date: date) -> bytes:
        if type(edition_date) is not date:
            raise ValueError("edition_date must be a date")
        user = _uuid(user_id, field_name="user_id")
        run = _uuid(global_run_id, field_name="global_run_id")
        return f"market-morning-delivery:v1:{user}:{run}:{edition_date.isoformat()}".encode(
            "ascii"
        )

    @classmethod
    def _token_for(
        cls,
        secret: bytes,
        *,
        user_id: str,
        global_run_id: str,
        edition_date: date,
    ) -> str:
        digest = hmac.new(
            secret,
            cls._identity(user_id, global_run_id, edition_date),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    async def token_digest_builder(
        self,
        user_id: str,
        global_run_id: str,
        edition_date: date,
    ) -> str:
        token = self._token_for(
            self._secrets[0],
            user_id=user_id,
            global_run_id=global_run_id,
            edition_date=edition_date,
        )
        return _token_digest(token)

    async def link_builder(self, context: EmailDeliveryContext) -> DeliveryDeepLink:
        if not isinstance(context, EmailDeliveryContext):
            raise EmailDeliveryPortUnavailable("delivery_link_unavailable")
        token = None
        for secret in self._secrets:
            candidate = self._token_for(
                secret,
                user_id=context.user_id,
                global_run_id=context.global_run_id,
                edition_date=context.edition_date,
            )
            if hmac.compare_digest(
                _token_digest(candidate),
                context.deep_link_token_sha256,
            ):
                token = candidate
                break
        if token is None:
            raise EmailDeliveryPortUnavailable("delivery_link_unavailable")
        return DeliveryDeepLink(
            url=f"{self._base_url}#token={token}",
            token=token,
        )


def build_delivery_link_signer_from_env(
    environ: Mapping[str, str] | None = None,
) -> DeliveryLinkSigner:
    """Build the signer directly from a worker secret environment."""

    values = os.environ if environ is None else environ
    base_url = values.get(DELIVERY_LINK_BASE_URL_ENV, "")
    secret = values.get(DELIVERY_LINK_SECRET_ENV, "")
    previous = values.get(DELIVERY_LINK_PREVIOUS_SECRET_ENV, "")
    try:
        return DeliveryLinkSigner(
            base_url=base_url,
            signing_secret=secret.encode("utf-8"),
            previous_signing_secret=(
                previous.encode("utf-8") if previous else None
            ),
        )
    except (AttributeError, UnicodeError, ValueError):
        raise DeliveryLinkConfigurationError("delivery_link_configuration_invalid") from None


def delivery_link_static_preflight_checks(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return only stable, non-sensitive deployment blocking codes."""

    values = os.environ if environ is None else environ
    if not values.get(DELIVERY_LINK_BASE_URL_ENV, "") or not values.get(
        DELIVERY_LINK_SECRET_ENV, ""
    ):
        return ("delivery_link_configuration_missing",)
    try:
        build_delivery_link_signer_from_env(values)
    except DeliveryLinkConfigurationError:
        return ("delivery_link_configuration_invalid",)
    return ()


def build_delivery_link_redemption_statement(
    *,
    user_id: str,
    token_sha256: str,
    requested_after: datetime,
):
    canonical_user = _uuid(user_id, field_name="user_id")
    if not isinstance(token_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", token_sha256
    ):
        raise ValueError("token_sha256 must be a SHA-256 digest")
    return (
        select(
            DeliveryAttemptRecord.delivery_attempt_id,
            DeliveryAttemptRecord.edition_date,
        )
        .where(
            DeliveryAttemptRecord.user_id == canonical_user,
            DeliveryAttemptRecord.deep_link_token_sha256 == token_sha256,
            DeliveryAttemptRecord.channel == "email",
            DeliveryAttemptRecord.status.in_(("sent", "delivered", "clicked")),
            DeliveryAttemptRecord.requested_at >= requested_after,
        )
        .order_by(DeliveryAttemptRecord.requested_at.desc())
        .limit(2)
    )


async def redeem_delivery_link(
    *,
    user_id: str,
    token: str,
    redeemed_at: datetime,
    max_age: timedelta = DELIVERY_LINK_MAX_AGE,
    session_factory: async_sessionmaker[AsyncSession] | Any | None = None,
) -> DeliveryLinkRedemption:
    """Validate a body-carried token against the authenticated product user."""

    canonical_user = _uuid(user_id, field_name="user_id")
    digest = _token_digest(token)
    redeemed = _mysql_utc(redeemed_at, field_name="redeemed_at")
    if not timedelta(hours=1) <= max_age <= timedelta(days=30):
        raise ValueError("max_age is invalid")
    factory = session_factory or get_session_factory()
    async with factory.begin() as session:
        rows = (
            await session.execute(
                build_delivery_link_redemption_statement(
                    user_id=canonical_user,
                    token_sha256=digest,
                    requested_after=redeemed - max_age,
                )
            )
        ).all()
    if len(rows) != 1:
        raise DeliveryLinkUnavailable("delivery_link_unavailable")
    edition_date = rows[0].edition_date
    return DeliveryLinkRedemption(edition_date=edition_date)


__all__ = [
    "DELIVERY_LINK_BASE_URL_ENV",
    "DELIVERY_LINK_DESTINATION_PATH",
    "DELIVERY_LINK_MAX_AGE",
    "DELIVERY_LINK_PREVIOUS_SECRET_ENV",
    "DELIVERY_LINK_SECRET_ENV",
    "DeliveryLinkConfigurationError",
    "DeliveryLinkRedemption",
    "DeliveryLinkSigner",
    "DeliveryLinkUnavailable",
    "build_delivery_link_redemption_statement",
    "build_delivery_link_signer_from_env",
    "delivery_link_static_preflight_checks",
    "redeem_delivery_link",
]
