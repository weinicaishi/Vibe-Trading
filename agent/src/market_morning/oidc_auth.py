"""Built-in fail-closed OIDC/JWKS adapters for Market Morning deployments."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import ipaddress
import json
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from types import MappingProxyType
from urllib.parse import urlsplit

import httpx
import jwt

from src.api.market_morning_admin_auth import (
    MarketMorningAdminAuthAdapter,
    MarketMorningAdminAuthenticationRejected,
    OperatorPermission,
    VerifiedMarketMorningOperator,
)
from src.api.market_morning_auth import (
    MarketMorningAuthenticationRejected,
    MarketMorningProductAuthAdapter,
    VerifiedMarketMorningIdentity,
)
from src.config.accessor import get_env_config
from src.market_morning.session_ledger import (
    BUILTIN_SESSION_VALIDATOR_FACTORY,
    pseudonymous_oidc_subject_reference,
)

BUILTIN_PRODUCT_AUTH_FACTORY = "src.market_morning.oidc_auth:build_product_auth_adapter"
BUILTIN_ADMIN_AUTH_FACTORY = "src.market_morning.oidc_auth:build_admin_auth_adapter"

_ASYMMETRIC_ALGORITHMS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES384",
        "ES512",
    }
)
_OPERATOR_PERMISSIONS = frozenset(
    {
        "operations.read",
        "content.review",
        "publication.control",
        "access.manage",
    }
)
_CLAIM_NAME_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_TOKEN_LENGTH = 8192
_MAX_JWKS_BYTES = 64 * 1024
_MAX_JWKS_KEYS = 64
_MAX_KEY_ID_LENGTH = 128
_UNKNOWN_KID_REFRESH_COOLDOWN_SECONDS = 5.0


class OidcConfigurationError(RuntimeError):
    """Safe deployment configuration error without secret values."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class OidcProviderUnavailable(RuntimeError):
    """The configured identity provider could not be safely consulted."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class OidcTokenRejected(RuntimeError):
    """A bearer token failed cryptographic, claim, or session validation."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


SessionValidator = Callable[
    [str, Mapping[str, Any]],
    bool | Awaitable[bool],
]
JwksFetcher = Callable[[], Awaitable[Mapping[str, Any]]]


def _freeze_claim(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_claim(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_claim(item) for item in value)
    return value


def _has_control(value: str) -> bool:
    return any(unicodedata.category(char).startswith("C") for char in value)


def _validate_https_endpoint(value: str, *, error_code: str) -> str:
    canonical = value.strip()
    try:
        parsed = urlsplit(canonical)
        port = parsed.port
    except ValueError as error:
        raise OidcConfigurationError(error_code) from error
    if (
        not canonical
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or _has_control(canonical)
    ):
        raise OidcConfigurationError(error_code)
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise OidcConfigurationError(error_code)
    return canonical


def _canonical_text(
    value: str,
    *,
    error_code: str,
    maximum: int,
) -> str:
    canonical = value.strip()
    if not canonical or len(canonical) > maximum or canonical != value or _has_control(canonical):
        raise OidcConfigurationError(error_code)
    return canonical


@dataclass(frozen=True, slots=True)
class OidcSettings:
    provider: str
    issuer: str
    jwks_url: str
    audience: str
    algorithm: str = "RS256"
    jwks_ttl_seconds: int = 300
    http_timeout_seconds: float = 5.0
    clock_skew_seconds: int = 30
    access_token_max_lifetime_seconds: int = 900
    admin_roles_claim: str = "roles"

    def __post_init__(self) -> None:
        provider = _canonical_text(
            self.provider,
            error_code="oidc_provider_invalid",
            maximum=64,
        )
        if not _PROVIDER_RE.fullmatch(provider):
            raise OidcConfigurationError("oidc_provider_invalid")
        _validate_https_endpoint(
            self.issuer,
            error_code="oidc_issuer_invalid",
        )
        _validate_https_endpoint(
            self.jwks_url,
            error_code="oidc_jwks_url_invalid",
        )
        _canonical_text(
            self.audience,
            error_code="oidc_audience_invalid",
            maximum=255,
        )
        if self.algorithm not in _ASYMMETRIC_ALGORITHMS:
            raise OidcConfigurationError("oidc_algorithm_invalid")
        if not 30 <= self.jwks_ttl_seconds <= 3600:
            raise OidcConfigurationError("oidc_jwks_ttl_invalid")
        if not 1.0 <= self.http_timeout_seconds <= 15.0:
            raise OidcConfigurationError("oidc_http_timeout_invalid")
        if not 0 <= self.clock_skew_seconds <= 120:
            raise OidcConfigurationError("oidc_clock_skew_invalid")
        if not 60 <= self.access_token_max_lifetime_seconds <= 1800:
            raise OidcConfigurationError("oidc_access_token_lifetime_invalid")
        if not _CLAIM_NAME_RE.fullmatch(self.admin_roles_claim):
            raise OidcConfigurationError("oidc_admin_roles_claim_invalid")

    @classmethod
    def from_environment(cls) -> "OidcSettings":
        cfg = get_env_config().market_morning
        return cls(
            provider=cfg.oidc_provider,
            issuer=cfg.oidc_issuer,
            jwks_url=cfg.oidc_jwks_url,
            audience=cfg.oidc_audience,
            algorithm=cfg.oidc_algorithm,
            jwks_ttl_seconds=cfg.oidc_jwks_ttl_seconds,
            http_timeout_seconds=cfg.oidc_http_timeout_seconds,
            clock_skew_seconds=cfg.oidc_clock_skew_seconds,
            access_token_max_lifetime_seconds=(cfg.oidc_access_token_max_lifetime_seconds),
            admin_roles_claim=cfg.oidc_admin_roles_claim,
        )


def _factory_parts(factory_path: str, *, error_code: str) -> tuple[str, str]:
    canonical = factory_path.strip()
    if not canonical or canonical.count(":") != 1:
        raise OidcConfigurationError(error_code)
    module_name, attribute_name = canonical.split(":", 1)
    if not module_name or not attribute_name:
        raise OidcConfigurationError(error_code)
    return module_name, attribute_name


def load_session_validator(factory_path: str) -> SessionValidator:
    module_name, attribute_name = _factory_parts(
        factory_path,
        error_code="oidc_session_validator_factory_invalid",
    )
    try:
        factory = getattr(importlib.import_module(module_name), attribute_name)
        validator = factory()
    except Exception as error:
        raise OidcConfigurationError("oidc_session_validator_factory_failed") from error
    if not callable(validator):
        raise OidcConfigurationError("oidc_session_validator_factory_contract_invalid")
    return validator


def parse_admin_role_permissions(raw_json: str) -> dict[str, frozenset[OperatorPermission]]:
    try:
        payload = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError) as error:
        raise OidcConfigurationError("oidc_admin_role_permissions_invalid") from error
    if not isinstance(payload, dict) or not payload or len(payload) > 128:
        raise OidcConfigurationError("oidc_admin_role_permissions_invalid")
    result: dict[str, frozenset[OperatorPermission]] = {}
    for role, permissions in payload.items():
        if (
            not isinstance(role, str)
            or not role
            or len(role) > 128
            or role != role.strip()
            or _has_control(role)
            or not isinstance(permissions, list)
            or not permissions
            or any(not isinstance(item, str) for item in permissions)
        ):
            raise OidcConfigurationError("oidc_admin_role_permissions_invalid")
        canonical_permissions = frozenset(permissions)
        if not canonical_permissions <= _OPERATOR_PERMISSIONS:
            raise OidcConfigurationError("oidc_admin_role_permissions_invalid")
        result[role] = canonical_permissions  # type: ignore[assignment]
    return result


class OidcTokenVerifier:
    """Verify signed JWTs with a bounded rotating JWKS cache."""

    def __init__(
        self,
        *,
        settings: OidcSettings,
        session_validator: SessionValidator,
        jwks_fetcher: JwksFetcher | None = None,
    ) -> None:
        if not callable(session_validator):
            raise OidcConfigurationError("oidc_session_validator_factory_contract_invalid")
        self.settings = settings
        self._session_validator = session_validator
        self._jwks_fetcher = jwks_fetcher or self._fetch_remote_jwks
        self._keys: dict[str, jwt.PyJWK] = {}
        self._cache_expires_at = 0.0
        self._next_unknown_kid_refresh_at = 0.0
        self._cache_lock = asyncio.Lock()

    async def _fetch_remote_jwks(self) -> Mapping[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.http_timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.get(
                    self.settings.jwks_url,
                    headers={"Accept": "application/json"},
                )
                if response.status_code != 200:
                    raise OidcProviderUnavailable("oidc_jwks_http_failed")
                declared_length = response.headers.get("content-length")
                if declared_length is not None and int(declared_length) > _MAX_JWKS_BYTES:
                    raise OidcProviderUnavailable("oidc_jwks_response_too_large")
                if len(response.content) > _MAX_JWKS_BYTES:
                    raise OidcProviderUnavailable("oidc_jwks_response_too_large")
                payload = response.json()
        except OidcProviderUnavailable:
            raise
        except Exception as error:
            raise OidcProviderUnavailable("oidc_jwks_unavailable") from error
        if not isinstance(payload, dict):
            raise OidcProviderUnavailable("oidc_jwks_contract_invalid")
        return payload

    def _parse_jwks(self, payload: Mapping[str, Any]) -> dict[str, jwt.PyJWK]:
        raw_keys = payload.get("keys")
        if not isinstance(raw_keys, list) or not raw_keys or len(raw_keys) > _MAX_JWKS_KEYS:
            raise OidcProviderUnavailable("oidc_jwks_contract_invalid")
        parsed: dict[str, jwt.PyJWK] = {}
        expected_key_type = "RSA" if self.settings.algorithm.startswith(("RS", "PS")) else "EC"
        for raw_key in raw_keys:
            if not isinstance(raw_key, dict):
                raise OidcProviderUnavailable("oidc_jwks_contract_invalid")
            if raw_key.get("use") not in {None, "sig"}:
                continue
            if raw_key.get("alg") not in {None, self.settings.algorithm}:
                continue
            if raw_key.get("kty") != expected_key_type:
                continue
            key_id = raw_key.get("kid")
            if (
                not isinstance(key_id, str)
                or not key_id
                or len(key_id) > _MAX_KEY_ID_LENGTH
                or key_id != key_id.strip()
                or _has_control(key_id)
            ):
                raise OidcProviderUnavailable("oidc_jwks_contract_invalid")
            if key_id in parsed:
                raise OidcProviderUnavailable("oidc_jwks_contract_invalid")
            try:
                parsed[key_id] = jwt.PyJWK.from_dict(
                    raw_key,
                    algorithm=self.settings.algorithm,
                )
            except Exception as error:
                raise OidcProviderUnavailable("oidc_jwks_contract_invalid") from error
        if not parsed:
            raise OidcProviderUnavailable("oidc_jwks_contract_invalid")
        return parsed

    async def _refresh_keys(self) -> None:
        payload = await self._jwks_fetcher()
        if not isinstance(payload, Mapping):
            raise OidcProviderUnavailable("oidc_jwks_contract_invalid")
        self._keys = self._parse_jwks(payload)
        self._cache_expires_at = time.monotonic() + self.settings.jwks_ttl_seconds

    async def _resolve_key(self, key_id: str) -> jwt.PyJWK:
        async with self._cache_lock:
            now = time.monotonic()
            refreshed = False
            if now >= self._cache_expires_at:
                await self._refresh_keys()
                refreshed = True
            key = self._keys.get(key_id)
            if key is not None:
                return key
            if refreshed or now < self._next_unknown_kid_refresh_at:
                raise OidcTokenRejected("oidc_signing_key_unknown")
            # A new kid is the normal key-rotation signal. Refresh exactly
            # once when the current cache is still fresh. The global cooldown
            # prevents arbitrary kid values from amplifying JWKS traffic.
            self._next_unknown_kid_refresh_at = now + _UNKNOWN_KID_REFRESH_COOLDOWN_SECONDS
            await self._refresh_keys()
            key = self._keys.get(key_id)
            if key is None:
                raise OidcTokenRejected("oidc_signing_key_unknown")
            return key

    async def verify_claims(self, token: str) -> Mapping[str, Any]:
        if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN_LENGTH or _has_control(token):
            raise OidcTokenRejected("oidc_token_invalid")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as error:
            raise OidcTokenRejected("oidc_token_invalid") from error
        algorithm = header.get("alg")
        key_id = header.get("kid")
        if algorithm != self.settings.algorithm:
            raise OidcTokenRejected("oidc_algorithm_mismatch")
        if (
            not isinstance(key_id, str)
            or not key_id
            or len(key_id) > _MAX_KEY_ID_LENGTH
            or key_id != key_id.strip()
            or _has_control(key_id)
        ):
            raise OidcTokenRejected("oidc_key_id_invalid")
        key = await self._resolve_key(key_id)
        try:
            claims = jwt.decode(
                token,
                key=key.key,
                algorithms=[self.settings.algorithm],
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                leeway=self.settings.clock_skew_seconds,
                options={
                    "require": [
                        "exp",
                        "iat",
                        "iss",
                        "aud",
                        "sub",
                        "auth_time",
                    ]
                },
            )
        except jwt.PyJWTError as error:
            raise OidcTokenRejected("oidc_claims_invalid") from error
        subject = claims.get("sub")
        audience = claims.get("aud")
        issued_at = claims.get("iat")
        expires_at = claims.get("exp")
        auth_time = claims.get("auth_time")
        if (
            not isinstance(subject, str)
            or not subject
            or len(subject) > 255
            or subject != subject.strip()
            or _has_control(subject)
            or audience != self.settings.audience
            or isinstance(issued_at, bool)
            or not isinstance(issued_at, (int, float))
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or isinstance(auth_time, bool)
            or not isinstance(auth_time, (int, float))
        ):
            raise OidcTokenRejected("oidc_claims_invalid")
        if (
            float(expires_at) <= float(issued_at)
            or float(expires_at) - float(issued_at) > self.settings.access_token_max_lifetime_seconds
        ):
            raise OidcTokenRejected("oidc_access_token_lifetime_invalid")
        try:
            authenticated_at = datetime.fromtimestamp(
                auth_time,
                tz=timezone.utc,
            )
        except (OverflowError, OSError, ValueError) as error:
            raise OidcTokenRejected("oidc_claims_invalid") from error
        if authenticated_at > datetime.now(timezone.utc) + timedelta(seconds=self.settings.clock_skew_seconds):
            raise OidcTokenRejected("oidc_claims_invalid")
        try:
            immutable_claims = _freeze_claim(claims)
            session_result = self._session_validator(token, immutable_claims)
            if inspect.isawaitable(session_result):
                session_result = await session_result
        except OidcTokenRejected:
            raise
        except Exception as error:
            raise OidcProviderUnavailable("oidc_session_validation_unavailable") from error
        if session_result is not True:
            raise OidcTokenRejected("oidc_session_inactive")
        return immutable_claims


def _authenticated_at(claims: Mapping[str, Any]) -> datetime:
    return datetime.fromtimestamp(float(claims["auth_time"]), tz=timezone.utc)


def build_product_auth_adapter(
    *,
    settings: OidcSettings | None = None,
    session_validator: SessionValidator | None = None,
    jwks_fetcher: JwksFetcher | None = None,
) -> MarketMorningProductAuthAdapter:
    resolved_settings = settings or OidcSettings.from_environment()
    validator = session_validator or load_session_validator(
        get_env_config().market_morning.oidc_session_validator_factory
    )
    verifier = OidcTokenVerifier(
        settings=resolved_settings,
        session_validator=validator,
        jwks_fetcher=jwks_fetcher,
    )

    async def _verify(token: str) -> VerifiedMarketMorningIdentity:
        try:
            claims = await verifier.verify_claims(token)
        except OidcTokenRejected as error:
            raise MarketMorningAuthenticationRejected(error.error_code) from error
        return VerifiedMarketMorningIdentity(
            external_subject=pseudonymous_oidc_subject_reference(
                resolved_settings.issuer,
                str(claims["sub"]),
            ),
            authenticated_at=_authenticated_at(claims),
        )

    async def _revoke(token: str) -> None:
        revoke_claims = getattr(validator, "revoke_claims", None)
        if not callable(revoke_claims):
            raise OidcProviderUnavailable("oidc_session_revocation_unavailable")
        try:
            claims = await verifier.verify_claims(token)
            result = revoke_claims(claims, reason="logout")
            if inspect.isawaitable(result):
                result = await result
        except OidcTokenRejected as error:
            raise MarketMorningAuthenticationRejected(error.error_code) from error
        if result is not True:
            raise MarketMorningAuthenticationRejected("oidc_session_inactive")

    return MarketMorningProductAuthAdapter(
        provider=resolved_settings.provider,
        verify_bearer=_verify,
        revoke_bearer=_revoke,
    )


def _roles(claims: Mapping[str, Any], claim_name: str) -> frozenset[str]:
    raw = claims.get(claim_name)
    values = (raw,) if isinstance(raw, str) else raw
    if not isinstance(values, (list, tuple)) or any(
        not isinstance(value, str) or not value or len(value) > 128 or value != value.strip() or _has_control(value)
        for value in values
    ):
        raise OidcTokenRejected("oidc_admin_roles_invalid")
    return frozenset(values)


def build_admin_auth_adapter(
    *,
    settings: OidcSettings | None = None,
    session_validator: SessionValidator | None = None,
    jwks_fetcher: JwksFetcher | None = None,
    role_permissions: Mapping[str, frozenset[OperatorPermission]] | None = None,
) -> MarketMorningAdminAuthAdapter:
    resolved_settings = settings or OidcSettings.from_environment()
    validator = session_validator or load_session_validator(
        get_env_config().market_morning.oidc_session_validator_factory
    )
    mapping = (
        dict(role_permissions)
        if role_permissions is not None
        else parse_admin_role_permissions(get_env_config().market_morning.oidc_admin_role_permissions_json)
    )
    if not mapping:
        raise OidcConfigurationError("oidc_admin_role_permissions_invalid")
    for role, permissions in mapping.items():
        if (
            not isinstance(role, str)
            or not role
            or len(role) > 128
            or role != role.strip()
            or _has_control(role)
            or not isinstance(permissions, frozenset)
            or not permissions
            or not permissions <= _OPERATOR_PERMISSIONS
        ):
            raise OidcConfigurationError("oidc_admin_role_permissions_invalid")
    verifier = OidcTokenVerifier(
        settings=resolved_settings,
        session_validator=validator,
        jwks_fetcher=jwks_fetcher,
    )

    async def _verify(token: str) -> VerifiedMarketMorningOperator:
        try:
            claims = await verifier.verify_claims(token)
            permissions: set[OperatorPermission] = set()
            for role in _roles(claims, resolved_settings.admin_roles_claim):
                permissions.update(mapping.get(role, frozenset()))
            if not permissions:
                raise OidcTokenRejected("oidc_admin_role_unauthorized")
        except OidcTokenRejected as error:
            raise MarketMorningAdminAuthenticationRejected(error.error_code) from error
        return VerifiedMarketMorningOperator(
            actor_reference=pseudonymous_oidc_subject_reference(
                resolved_settings.issuer,
                str(claims["sub"]),
            ),
            permissions=frozenset(permissions),
        )

    async def _revoke(token: str) -> None:
        revoke_claims = getattr(validator, "revoke_claims", None)
        if not callable(revoke_claims):
            raise OidcProviderUnavailable("oidc_session_revocation_unavailable")
        try:
            claims = await verifier.verify_claims(token)
            result = revoke_claims(claims, reason="operator_logout")
            if inspect.isawaitable(result):
                result = await result
        except OidcTokenRejected as error:
            raise MarketMorningAdminAuthenticationRejected(error.error_code) from error
        if result is not True:
            raise MarketMorningAdminAuthenticationRejected("oidc_session_inactive")

    return MarketMorningAdminAuthAdapter(
        provider=resolved_settings.provider,
        verify_bearer=_verify,
        revoke_bearer=_revoke,
    )


def builtin_oidc_preflight_checks(config: Any) -> tuple[str, ...]:
    """Return stable static blocking codes without exposing config values."""

    product_builtin = config.auth_factory.strip() == BUILTIN_PRODUCT_AUTH_FACTORY
    admin_builtin = config.admin_auth_factory.strip() == BUILTIN_ADMIN_AUTH_FACTORY
    if not product_builtin and not admin_builtin:
        return ()
    checks: list[str] = []
    if not config.oidc_issuer.strip():
        checks.append("oidc_issuer_missing")
    if not config.oidc_jwks_url.strip():
        checks.append("oidc_jwks_url_missing")
    if not config.oidc_audience.strip():
        checks.append("oidc_audience_missing")
    if not config.oidc_session_validator_factory.strip():
        checks.append("oidc_session_validator_factory_missing")
    if (
        config.oidc_session_validator_factory.strip() == BUILTIN_SESSION_VALIDATOR_FACTORY
        and not config.oidc_session_claim.strip()
    ):
        checks.append("oidc_session_claim_missing")
    if admin_builtin and not config.oidc_admin_role_permissions_json.strip():
        checks.append("oidc_admin_role_permissions_missing")
    if not checks:
        try:
            OidcSettings(
                provider=config.oidc_provider,
                issuer=config.oidc_issuer,
                jwks_url=config.oidc_jwks_url,
                audience=config.oidc_audience,
                algorithm=config.oidc_algorithm,
                jwks_ttl_seconds=config.oidc_jwks_ttl_seconds,
                http_timeout_seconds=config.oidc_http_timeout_seconds,
                clock_skew_seconds=config.oidc_clock_skew_seconds,
                access_token_max_lifetime_seconds=(config.oidc_access_token_max_lifetime_seconds),
                admin_roles_claim=config.oidc_admin_roles_claim,
            )
        except OidcConfigurationError:
            checks.append("oidc_configuration_invalid")
        try:
            _factory_parts(
                config.oidc_session_validator_factory,
                error_code="oidc_session_validator_factory_invalid",
            )
        except OidcConfigurationError:
            checks.append("oidc_session_validator_factory_invalid")
        if admin_builtin:
            try:
                parse_admin_role_permissions(config.oidc_admin_role_permissions_json)
            except OidcConfigurationError:
                checks.append("oidc_admin_role_permissions_invalid")
    return tuple(sorted(checks))


__all__ = [
    "BUILTIN_ADMIN_AUTH_FACTORY",
    "BUILTIN_PRODUCT_AUTH_FACTORY",
    "OidcConfigurationError",
    "OidcProviderUnavailable",
    "OidcSettings",
    "OidcTokenRejected",
    "OidcTokenVerifier",
    "build_admin_auth_adapter",
    "build_product_auth_adapter",
    "builtin_oidc_preflight_checks",
    "load_session_validator",
    "parse_admin_role_permissions",
]
