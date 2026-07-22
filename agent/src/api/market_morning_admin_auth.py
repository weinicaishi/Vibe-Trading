"""Deployment-owned identity and least-privilege boundary for operators."""

from __future__ import annotations

import importlib
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.api.security import require_auth as require_legacy_api_auth
from src.config.accessor import get_env_config

logger = logging.getLogger(__name__)

OperatorPermission = Literal[
    "operations.read",
    "content.review",
    "publication.control",
    "access.manage",
]
_OPERATOR_PERMISSIONS = frozenset(
    {
        "operations.read",
        "content.review",
        "publication.control",
        "access.manage",
    }
)
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_BEARER_LENGTH = 8192
_LEGACY_ACTOR_REFERENCE = "vibe-api-key-operator"
_bearer = HTTPBearer(auto_error=False)


class AdminAuthConfigurationError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class MarketMorningAdminAuthenticationRejected(RuntimeError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


@dataclass(frozen=True, slots=True)
class VerifiedMarketMorningOperator:
    actor_reference: str
    permissions: frozenset[OperatorPermission]

    def __post_init__(self) -> None:
        actor = self.actor_reference
        if (
            not isinstance(actor, str)
            or not actor
            or len(actor) > 128
            or actor != actor.strip()
            or any(unicodedata.category(char).startswith("C") for char in actor)
        ):
            raise AdminAuthConfigurationError("admin_auth_identity_contract_invalid")
        permissions = self.permissions
        if not isinstance(permissions, frozenset) or not permissions or not permissions <= _OPERATOR_PERMISSIONS:
            raise AdminAuthConfigurationError("admin_auth_identity_contract_invalid")


AdminBearerVerifier = Callable[
    [str],
    Awaitable[VerifiedMarketMorningOperator],
]
AdminBearerRevoker = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class MarketMorningAdminAuthAdapter:
    provider: str
    verify_bearer: AdminBearerVerifier
    revoke_bearer: AdminBearerRevoker | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.provider, str)
            or not _PROVIDER_RE.fullmatch(self.provider)
            or not callable(self.verify_bearer)
            or (self.revoke_bearer is not None and not callable(self.revoke_bearer))
        ):
            raise AdminAuthConfigurationError("admin_auth_factory_contract_invalid")


def load_admin_auth_adapter(factory_path: str) -> MarketMorningAdminAuthAdapter:
    normalized = factory_path.strip()
    if not normalized or normalized.count(":") != 1:
        raise AdminAuthConfigurationError("admin_auth_factory_invalid")
    module_name, attribute_name = normalized.split(":", 1)
    if not module_name or not attribute_name:
        raise AdminAuthConfigurationError("admin_auth_factory_invalid")
    try:
        factory = getattr(importlib.import_module(module_name), attribute_name)
        adapter = factory()
    except Exception as error:
        logger.error(
            "Market Morning admin auth factory failed: exception_type=%s",
            type(error).__name__,
        )
        raise AdminAuthConfigurationError("admin_auth_factory_failed") from None
    if not isinstance(adapter, MarketMorningAdminAuthAdapter):
        raise AdminAuthConfigurationError("admin_auth_factory_contract_invalid")
    return adapter


@lru_cache(maxsize=8)
def _load_configured_adapter(factory_path: str) -> MarketMorningAdminAuthAdapter:
    return load_admin_auth_adapter(factory_path)


def clear_admin_auth_adapter_cache() -> None:
    _load_configured_adapter.cache_clear()


def configured_admin_auth_adapter_cache_info():
    return _load_configured_adapter.cache_info()


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Market Morning operator authentication failed",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _verified_operator(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> VerifiedMarketMorningOperator:
    config = get_env_config().market_morning
    factory_path = config.admin_auth_factory.strip()
    if not factory_path:
        await require_legacy_api_auth(request, credentials)
        return VerifiedMarketMorningOperator(
            actor_reference=_LEGACY_ACTOR_REFERENCE,
            permissions=frozenset(_OPERATOR_PERMISSIONS),
        )

    try:
        adapter = _load_configured_adapter(factory_path)
    except Exception as error:
        logger.warning(
            "Market Morning admin auth adapter unavailable: exception_type=%s",
            type(error).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=("Market Morning operator authentication is temporarily unavailable"),
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
        operator = await adapter.verify_bearer(token)
    except MarketMorningAdminAuthenticationRejected as error:
        raise _unauthorized() from error
    except Exception as error:
        logger.warning(
            "Market Morning operator verification failed: provider=%s exception_type=%s",
            adapter.provider,
            type(error).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=("Market Morning operator authentication is temporarily unavailable"),
        ) from error
    if not isinstance(operator, VerifiedMarketMorningOperator):
        logger.warning(
            "Market Morning operator identity contract failed: provider=%s",
            adapter.provider,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=("Market Morning operator authentication is temporarily unavailable"),
        )
    return operator


async def _require_permission(
    permission: OperatorPermission,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> VerifiedMarketMorningOperator:
    operator = await _verified_operator(request, credentials)
    if permission not in operator.permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Market Morning operator permission is unavailable",
        )
    return operator


async def revoke_current_operator_session(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> None:
    await _verified_operator(request, credentials)
    config = get_env_config().market_morning
    factory_path = config.admin_auth_factory.strip()
    if not factory_path or credentials is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=("Market Morning operator session revocation is temporarily unavailable"),
        )
    adapter = _load_configured_adapter(factory_path)
    if adapter.revoke_bearer is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=("Market Morning operator session revocation is temporarily unavailable"),
        )
    try:
        await adapter.revoke_bearer(credentials.credentials)
    except MarketMorningAdminAuthenticationRejected as error:
        raise _unauthorized() from error
    except Exception as error:
        logger.warning(
            "Market Morning operator session revocation failed: provider=%s exception_type=%s",
            adapter.provider,
            type(error).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=("Market Morning operator session revocation is temporarily unavailable"),
        ) from error


async def require_operations_reader(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> VerifiedMarketMorningOperator:
    return await _require_permission("operations.read", request, credentials)


async def require_content_reviewer(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> VerifiedMarketMorningOperator:
    return await _require_permission("content.review", request, credentials)


async def require_publication_controller(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> VerifiedMarketMorningOperator:
    return await _require_permission("publication.control", request, credentials)


async def require_access_manager(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> VerifiedMarketMorningOperator:
    return await _require_permission("access.manage", request, credentials)


__all__ = [
    "AdminAuthConfigurationError",
    "AdminBearerVerifier",
    "AdminBearerRevoker",
    "MarketMorningAdminAuthAdapter",
    "MarketMorningAdminAuthenticationRejected",
    "OperatorPermission",
    "VerifiedMarketMorningOperator",
    "clear_admin_auth_adapter_cache",
    "configured_admin_auth_adapter_cache_info",
    "load_admin_auth_adapter",
    "require_access_manager",
    "require_content_reviewer",
    "require_operations_reader",
    "require_publication_controller",
    "revoke_current_operator_session",
]
