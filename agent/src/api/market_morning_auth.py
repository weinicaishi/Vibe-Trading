"""Fail-closed product-authentication boundary for Market Morning."""

from __future__ import annotations

import importlib
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from src.config.accessor import get_env_config
from src.market_morning.db import get_session_factory
from src.market_morning.models import User

logger = logging.getLogger(__name__)

_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_BEARER_LENGTH = 8192
_RECENT_AUTH_MAX_AGE = timedelta(minutes=5)
_AUTH_TIME_FUTURE_SKEW = timedelta(minutes=1)
_ENTITLED_SUBSCRIPTION_STATUSES = (
    "active",
    "private_beta",
    "trialing",
)
_bearer = HTTPBearer(auto_error=False)


class ProductAuthConfigurationError(RuntimeError):
    """Safe adapter/configuration failure with no provider details."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class MarketMorningAuthenticationRejected(RuntimeError):
    """A deployment verifier rejected a bearer token."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


@dataclass(frozen=True, slots=True)
class VerifiedMarketMorningIdentity:
    """Opaque stable subject returned only after deployment verification."""

    external_subject: str
    authenticated_at: datetime | None = None

    def __post_init__(self) -> None:
        value = self.external_subject
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 255
            or value != value.strip()
            or any(unicodedata.category(char).startswith("C") for char in value)
        ):
            raise ProductAuthConfigurationError("product_auth_identity_contract_invalid")
        if self.authenticated_at is not None and (
            self.authenticated_at.tzinfo is None or self.authenticated_at.utcoffset() is None
        ):
            raise ProductAuthConfigurationError("product_auth_identity_contract_invalid")


ProductBearerVerifier = Callable[
    [str],
    Awaitable[VerifiedMarketMorningIdentity],
]
ProductBearerRevoker = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class MarketMorningProductAuthAdapter:
    """Deployment-owned OIDC/JWT verification port and optional DB factory."""

    provider: str
    verify_bearer: ProductBearerVerifier
    revoke_bearer: ProductBearerRevoker | None = None
    session_factory: Any | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.provider, str)
            or not _PROVIDER_RE.fullmatch(self.provider)
            or not callable(self.verify_bearer)
            or (self.revoke_bearer is not None and not callable(self.revoke_bearer))
        ):
            raise ProductAuthConfigurationError("product_auth_factory_contract_invalid")


@dataclass(frozen=True, slots=True)
class MarketMorningPrincipal:
    """Verified and currently entitled product user."""

    user_id: str
    external_subject: str
    authenticated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MarketMorningOnboardingPrincipal:
    """Verified identity before a Market Morning user row exists."""

    external_subject: str


def load_product_auth_adapter(
    factory_path: str,
) -> MarketMorningProductAuthAdapter:
    """Load a deployment-owned ``module:function`` product-auth adapter."""

    normalized = factory_path.strip()
    if not normalized or normalized.count(":") != 1:
        raise ProductAuthConfigurationError("product_auth_factory_invalid")
    module_name, attribute_name = normalized.split(":", 1)
    if not module_name or not attribute_name:
        raise ProductAuthConfigurationError("product_auth_factory_invalid")
    try:
        factory = getattr(importlib.import_module(module_name), attribute_name)
        adapter = factory()
    except Exception as error:
        logger.error(
            "Market Morning product auth factory failed: exception_type=%s",
            type(error).__name__,
        )
        raise ProductAuthConfigurationError("product_auth_factory_failed") from None
    if not isinstance(adapter, MarketMorningProductAuthAdapter):
        raise ProductAuthConfigurationError("product_auth_factory_contract_invalid")
    return adapter


@lru_cache(maxsize=8)
def _load_configured_adapter(
    factory_path: str,
) -> MarketMorningProductAuthAdapter:
    return load_product_auth_adapter(factory_path)


def clear_product_auth_adapter_cache() -> None:
    _load_configured_adapter.cache_clear()


def configured_product_auth_adapter_cache_info():
    return _load_configured_adapter.cache_info()


def _resolve_configured_adapter() -> MarketMorningProductAuthAdapter:
    factory_path = get_env_config().market_morning.auth_factory.strip()
    if not factory_path:
        raise ProductAuthConfigurationError("product_auth_factory_not_configured")
    return _load_configured_adapter(factory_path)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Market Morning authentication failed",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _verified_identity(
    credentials: HTTPAuthorizationCredentials | None,
) -> tuple[MarketMorningProductAuthAdapter, VerifiedMarketMorningIdentity]:
    if not get_env_config().market_morning.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Market Morning is disabled",
        )
    try:
        adapter = _resolve_configured_adapter()
    except Exception as error:
        logger.warning(
            "Market Morning product auth adapter unavailable: exception_type=%s",
            type(error).__name__,
        )
        detail = (
            "Market Morning product authentication is not configured"
            if isinstance(error, ProductAuthConfigurationError)
            and error.error_code == "product_auth_factory_not_configured"
            else "Market Morning product authentication is temporarily unavailable"
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=detail,
        ) from error
    if credentials is None:
        raise _unauthorized()
    token = credentials.credentials
    if (
        not token
        or len(token) > _MAX_BEARER_LENGTH
        or any(unicodedata.category(char).startswith("C") for char in token)
    ):
        raise _unauthorized()

    try:
        identity = await adapter.verify_bearer(token)
    except MarketMorningAuthenticationRejected as error:
        raise _unauthorized() from error
    except Exception as error:
        logger.warning(
            "Market Morning product identity verification failed: provider=%s exception_type=%s",
            adapter.provider,
            type(error).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=("Market Morning product authentication is temporarily unavailable"),
        ) from error
    if not isinstance(identity, VerifiedMarketMorningIdentity):
        logger.warning(
            "Market Morning product identity contract failed: provider=%s",
            adapter.provider,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=("Market Morning product authentication is temporarily unavailable"),
        )
    return adapter, identity


async def revoke_current_product_session(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> None:
    adapter, _identity = await _verified_identity(credentials)
    if credentials is None:
        raise _unauthorized()
    if adapter.revoke_bearer is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Market Morning session revocation is temporarily unavailable",
        )
    try:
        await adapter.revoke_bearer(credentials.credentials)
    except MarketMorningAuthenticationRejected as error:
        raise _unauthorized() from error
    except Exception as error:
        logger.warning(
            "Market Morning product session revocation failed: provider=%s exception_type=%s",
            adapter.provider,
            type(error).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Market Morning session revocation is temporarily unavailable",
        ) from error


def _build_product_principal_statement(
    *,
    external_subject: str,
    account_statuses: tuple[str, ...],
):
    account_status_predicate = (
        User.account_status == account_statuses[0]
        if len(account_statuses) == 1
        else User.account_status.in_(account_statuses)
    )
    return select(User.user_id, User.external_subject).where(
        User.external_subject == external_subject,
        account_status_predicate,
        User.deleted_at.is_(None),
        User.trial_or_subscription_status.in_(_ENTITLED_SUBSCRIPTION_STATUSES),
    )


def build_active_product_principal_statement(*, external_subject: str):
    return _build_product_principal_statement(
        external_subject=external_subject,
        account_statuses=("active",),
    )


def build_account_deletion_principal_statement(*, external_subject: str):
    return _build_product_principal_statement(
        external_subject=external_subject,
        account_statuses=("active", "deletion_pending"),
    )


async def resolve_active_product_principal(
    session,
    *,
    external_subject: str,
    authenticated_at: datetime | None = None,
    allow_deletion_pending: bool = False,
) -> MarketMorningPrincipal | None:
    row = (
        (
            await session.execute(
                (
                    build_account_deletion_principal_statement(external_subject=external_subject)
                    if allow_deletion_pending
                    else build_active_product_principal_statement(external_subject=external_subject)
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return MarketMorningPrincipal(
        user_id=str(row["user_id"]),
        external_subject=str(row["external_subject"]),
        authenticated_at=authenticated_at,
    )


async def _resolve_verified_product_principal(
    adapter: MarketMorningProductAuthAdapter,
    identity: VerifiedMarketMorningIdentity,
    *,
    allow_deletion_pending: bool = False,
) -> MarketMorningPrincipal:
    factory = adapter.session_factory if adapter.session_factory is not None else get_session_factory()
    try:
        async with factory() as session:
            principal = await resolve_active_product_principal(
                session,
                external_subject=identity.external_subject,
                authenticated_at=identity.authenticated_at,
                allow_deletion_pending=allow_deletion_pending,
            )
    except Exception as error:
        logger.warning(
            "Market Morning product principal lookup failed: exception_type=%s",
            type(error).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Market Morning product authentication is temporarily unavailable",
        ) from error
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Market Morning access is unavailable",
        )
    return principal


async def require_market_morning_onboarding_principal(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> MarketMorningOnboardingPrincipal:
    """Verify identity without requiring an existing product user row."""

    _adapter, identity = await _verified_identity(credentials)
    return MarketMorningOnboardingPrincipal(external_subject=identity.external_subject)


async def require_market_morning_principal(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> MarketMorningPrincipal:
    """Verify identity and resolve a currently active, entitled account."""

    adapter, identity = await _verified_identity(credentials)
    return await _resolve_verified_product_principal(adapter, identity)


async def _recent_verified_identity(
    credentials: HTTPAuthorizationCredentials | None,
) -> tuple[MarketMorningProductAuthAdapter, VerifiedMarketMorningIdentity]:
    adapter, identity = await _verified_identity(credentials)
    authenticated_at = identity.authenticated_at
    now = datetime.now(timezone.utc)
    if (
        authenticated_at is None
        or authenticated_at > now + _AUTH_TIME_FUTURE_SKEW
        or now - authenticated_at.astimezone(timezone.utc) > _RECENT_AUTH_MAX_AGE
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Recent authentication is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return adapter, identity


async def require_recent_market_morning_principal(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> MarketMorningPrincipal:
    """Require provider-verified authentication no older than five minutes."""

    adapter, identity = await _recent_verified_identity(credentials)
    return await _resolve_verified_product_principal(adapter, identity)


async def require_account_deletion_principal(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> MarketMorningPrincipal:
    """Require recent auth while preserving idempotent deletion retries."""

    adapter, identity = await _recent_verified_identity(credentials)
    return await _resolve_verified_product_principal(
        adapter,
        identity,
        allow_deletion_pending=True,
    )


__all__ = [
    "MarketMorningAuthenticationRejected",
    "MarketMorningOnboardingPrincipal",
    "MarketMorningPrincipal",
    "MarketMorningProductAuthAdapter",
    "ProductAuthConfigurationError",
    "ProductBearerRevoker",
    "ProductBearerVerifier",
    "VerifiedMarketMorningIdentity",
    "build_account_deletion_principal_statement",
    "build_active_product_principal_statement",
    "clear_product_auth_adapter_cache",
    "configured_product_auth_adapter_cache_info",
    "load_product_auth_adapter",
    "require_account_deletion_principal",
    "require_market_morning_onboarding_principal",
    "require_market_morning_principal",
    "require_recent_market_morning_principal",
    "revoke_current_product_session",
    "resolve_active_product_principal",
]
