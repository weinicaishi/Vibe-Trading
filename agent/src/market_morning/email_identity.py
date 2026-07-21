"""Fail-closed deployment boundary for resolving verified email destinations.

The product database deliberately stores no email address.  A deployment-owned
identity directory resolves the internal user ID plus pseudonymous OIDC subject
at delivery time.  The core validates that the directory response is bound to
both identifiers before a destination can reach the email provider.
"""

from __future__ import annotations

import importlib
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import lru_cache
from uuid import UUID

from src.config.accessor import get_env_config
from src.market_morning.email_delivery_service import (
    EmailDeliveryPortUnavailable,
    EmailIdentityResolver,
)

logger = logging.getLogger(__name__)

EMAIL_IDENTITY_FACTORY_ENV = "VIBE_MARKET_MORNING_EMAIL_IDENTITY_FACTORY"
_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class EmailIdentityConfigurationError(RuntimeError):
    """Stable factory/configuration failure without directory details."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class EmailIdentityDirectoryUnavailable(RuntimeError):
    """Deployment adapter signal for a transient directory failure."""


def _uuid(value: str, *, field_name: str) -> str:
    try:
        return str(UUID(value))
    except (AttributeError, TypeError, ValueError):
        raise ValueError(f"{field_name} must be a UUID") from None


def _reference(value: str, *, field_name: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or value != value.strip()
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise ValueError(f"{field_name} is invalid")
    return value


def _email(value: str) -> str:
    canonical = _reference(value, field_name="email", maximum=320)
    if any(character.isspace() for character in canonical) or canonical.count("@") != 1:
        raise ValueError("email is invalid")
    local, domain = canonical.rsplit("@", 1)
    if (
        not local
        or len(local.encode("utf-8")) > 64
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or not domain
        or domain.startswith(".")
        or domain.endswith(".")
        or ".." in domain
    ):
        raise ValueError("email is invalid")
    try:
        ascii_domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("email is invalid") from None
    if (
        len(ascii_domain) > 253
        or "." not in ascii_domain
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or re.fullmatch(r"[a-z0-9-]+", label) is None
            for label in ascii_domain.split(".")
        )
    ):
        raise ValueError("email is invalid")
    return f"{local}@{ascii_domain}"


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedEmailIdentity:
    """One directory result explicitly bound to the requested product user."""

    user_id: str
    external_subject: str
    email: str | None = field(repr=False)
    email_verified: bool
    active: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", _uuid(self.user_id, field_name="user_id"))
        object.__setattr__(
            self,
            "external_subject",
            _reference(
                self.external_subject,
                field_name="external_subject",
                maximum=255,
            ),
        )
        if type(self.email_verified) is not bool or type(self.active) is not bool:
            raise ValueError("identity status is invalid")
        if self.email is None:
            if self.active and self.email_verified:
                raise ValueError("verified active identity requires an email")
        else:
            object.__setattr__(self, "email", _email(self.email))

    def __repr__(self) -> str:
        return "<VerifiedEmailIdentity redacted>"


EmailDirectoryResolver = Callable[
    [str, str],
    Awaitable[VerifiedEmailIdentity | None],
]


@dataclass(frozen=True, slots=True)
class MarketMorningEmailIdentityAdapter:
    """Deployment-owned verified directory lookup port."""

    provider: str
    resolve_identity: EmailDirectoryResolver = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.provider, str)
            or _PROVIDER.fullmatch(self.provider) is None
            or not callable(self.resolve_identity)
        ):
            raise EmailIdentityConfigurationError(
                "email_identity_factory_contract_invalid"
            )

    def __repr__(self) -> str:
        return f"<MarketMorningEmailIdentityAdapter provider={self.provider!r}>"


def _factory_path(value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 512
        or normalized.count(":") != 1
        or any(character.isspace() for character in normalized)
    ):
        raise EmailIdentityConfigurationError("email_identity_factory_invalid")
    module_name, attribute_name = normalized.split(":", 1)
    if not module_name or not attribute_name:
        raise EmailIdentityConfigurationError("email_identity_factory_invalid")
    return normalized


def load_email_identity_adapter(
    factory_path: str,
) -> MarketMorningEmailIdentityAdapter:
    """Load one deployment-owned ``module:function`` directory adapter."""

    normalized = _factory_path(factory_path)
    module_name, attribute_name = normalized.split(":", 1)
    try:
        factory = getattr(importlib.import_module(module_name), attribute_name)
        adapter = factory()
    except Exception as error:
        logger.error(
            "Market Morning email identity factory failed: exception_type=%s",
            type(error).__name__,
        )
        raise EmailIdentityConfigurationError(
            "email_identity_factory_failed"
        ) from None
    if not isinstance(adapter, MarketMorningEmailIdentityAdapter):
        raise EmailIdentityConfigurationError(
            "email_identity_factory_contract_invalid"
        )
    return adapter


@lru_cache(maxsize=8)
def _load_cached_adapter(factory_path: str) -> MarketMorningEmailIdentityAdapter:
    return load_email_identity_adapter(factory_path)


def clear_email_identity_adapter_cache() -> None:
    _load_cached_adapter.cache_clear()


def build_email_identity_resolver(
    factory_path: str | None = None,
) -> EmailIdentityResolver:
    """Build the validated core resolver used by ``run_email_delivery``."""

    configured_path = (
        get_env_config().market_morning.email_identity_factory
        if factory_path is None
        else factory_path
    )
    if not configured_path.strip():
        raise EmailIdentityConfigurationError("email_identity_factory_missing")
    adapter = _load_cached_adapter(_factory_path(configured_path))

    async def resolve(user_id: str, external_subject: str) -> str:
        canonical_user = _uuid(user_id, field_name="user_id")
        canonical_subject = _reference(
            external_subject,
            field_name="external_subject",
            maximum=255,
        )
        try:
            identity = await adapter.resolve_identity(
                canonical_user,
                canonical_subject,
            )
        except EmailIdentityDirectoryUnavailable:
            raise EmailDeliveryPortUnavailable("email_identity_unavailable") from None
        except Exception:
            raise EmailDeliveryPortUnavailable("email_identity_unavailable") from None
        if identity is None:
            raise ValueError("email identity is unavailable")
        if not isinstance(identity, VerifiedEmailIdentity):
            raise EmailDeliveryPortUnavailable("email_identity_unavailable")
        if (
            identity.user_id != canonical_user
            or identity.external_subject != canonical_subject
        ):
            raise EmailDeliveryPortUnavailable("email_identity_unavailable")
        if not identity.active or not identity.email_verified or identity.email is None:
            raise ValueError("email destination is unavailable")
        return identity.email

    return resolve


def email_identity_static_preflight_checks(factory_path: str) -> tuple[str, ...]:
    """Validate only the non-secret factory-path shape without importing it."""

    if not factory_path.strip():
        return ("email_identity_factory_missing",)
    try:
        _factory_path(factory_path)
    except EmailIdentityConfigurationError:
        return ("email_identity_factory_invalid",)
    return ()


__all__ = [
    "EMAIL_IDENTITY_FACTORY_ENV",
    "EmailDirectoryResolver",
    "EmailIdentityConfigurationError",
    "EmailIdentityDirectoryUnavailable",
    "MarketMorningEmailIdentityAdapter",
    "VerifiedEmailIdentity",
    "build_email_identity_resolver",
    "clear_email_identity_adapter_cache",
    "email_identity_static_preflight_checks",
    "load_email_identity_adapter",
]
