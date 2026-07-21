"""Cryptographic and deployment contracts for built-in Market Morning OIDC."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://identity.example.com/market-morning"
JWKS_URL = "https://identity.example.com/market-morning/jwks"
AUDIENCE = "market-morning-api"
SUBJECT = "private-provider-subject-17"


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(private_key, *, kid: str) -> dict:
    payload = jwt.algorithms.RSAAlgorithm.to_jwk(
        private_key.public_key(),
        as_dict=True,
    )
    payload.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return payload


def _claims(**overrides) -> dict:
    now = int(time.time())
    values = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": SUBJECT,
        "iat": now - 5,
        "auth_time": now - 30,
        "exp": now + 300,
        "sid": "provider-session-17",
    }
    values.update(overrides)
    return values


def _token(private_key, *, kid: str = "key-1", claims=None) -> str:
    return jwt.encode(
        _claims() if claims is None else claims,
        private_key,
        algorithm="RS256",
        headers={"kid": kid},
    )


def _settings():
    from src.market_morning.oidc_auth import OidcSettings

    return OidcSettings(
        provider="test-oidc",
        issuer=ISSUER,
        jwks_url=JWKS_URL,
        audience=AUDIENCE,
    )


async def _active_session(_token: str, _claims) -> bool:
    return True


def test_product_adapter_verifies_signature_claims_and_returns_pseudonym() -> None:
    from src.market_morning.oidc_auth import build_product_auth_adapter

    private_key = _key()

    async def fetch():
        return {"keys": [_jwk(private_key, kid="key-1")]}

    adapter = build_product_auth_adapter(
        settings=_settings(),
        session_validator=_active_session,
        jwks_fetcher=fetch,
    )
    identity = asyncio.run(adapter.verify_bearer(_token(private_key)))

    assert identity.external_subject.startswith("oidc:")
    assert len(identity.external_subject) == 69
    assert SUBJECT not in identity.external_subject
    assert identity.authenticated_at is not None
    assert identity.authenticated_at.tzinfo is not None


@pytest.mark.parametrize(
    "claims",
    [
        _claims(aud="another-api"),
        _claims(aud=[AUDIENCE, "another-api"]),
        _claims(exp=int(time.time()) - 300),
        {key: value for key, value in _claims().items() if key != "auth_time"},
        _claims(auth_time=True),
    ],
)
def test_product_adapter_rejects_invalid_required_claims(claims) -> None:
    from src.api.market_morning_auth import MarketMorningAuthenticationRejected
    from src.market_morning.oidc_auth import build_product_auth_adapter

    private_key = _key()

    async def fetch():
        return {"keys": [_jwk(private_key, kid="key-1")]}

    adapter = build_product_auth_adapter(
        settings=_settings(),
        session_validator=_active_session,
        jwks_fetcher=fetch,
    )

    with pytest.raises(MarketMorningAuthenticationRejected):
        asyncio.run(
            adapter.verify_bearer(_token(private_key, claims=claims))
        )


def test_algorithm_confusion_is_rejected_before_jwks_fetch() -> None:
    from src.api.market_morning_auth import MarketMorningAuthenticationRejected
    from src.market_morning.oidc_auth import build_product_auth_adapter

    calls = []

    async def fetch():
        calls.append("fetch")
        return {"keys": [_jwk(_key(), kid="key-1")]}

    adapter = build_product_auth_adapter(
        settings=_settings(),
        session_validator=_active_session,
        jwks_fetcher=fetch,
    )
    token = jwt.encode(
        _claims(),
        "not-an-asymmetric-key-but-long-enough-for-hs256",
        algorithm="HS256",
        headers={"kid": "key-1"},
    )

    with pytest.raises(MarketMorningAuthenticationRejected):
        asyncio.run(adapter.verify_bearer(token))
    assert calls == []


def test_unknown_kid_forces_one_jwks_refresh_for_rotation() -> None:
    from src.market_morning.oidc_auth import build_product_auth_adapter

    first_key = _key()
    rotated_key = _key()
    payloads = [
        {"keys": [_jwk(first_key, kid="key-1")]},
        {"keys": [_jwk(rotated_key, kid="key-2")]},
    ]
    calls = []

    async def fetch():
        calls.append("fetch")
        return payloads[len(calls) - 1]

    adapter = build_product_auth_adapter(
        settings=_settings(),
        session_validator=_active_session,
        jwks_fetcher=fetch,
    )

    asyncio.run(adapter.verify_bearer(_token(first_key, kid="key-1")))
    asyncio.run(adapter.verify_bearer(_token(rotated_key, kid="key-2")))
    assert calls == ["fetch", "fetch"]


def test_unknown_kid_refresh_is_bounded_and_mixed_jwks_keys_are_ignored() -> None:
    from src.api.market_morning_auth import MarketMorningAuthenticationRejected
    from src.market_morning.oidc_auth import build_product_auth_adapter

    private_key = _key()
    calls = []

    async def fetch():
        calls.append("fetch")
        return {
            "keys": [
                {"kty": "oct", "use": "enc"},
                _jwk(private_key, kid="key-1"),
            ]
        }

    adapter = build_product_auth_adapter(
        settings=_settings(),
        session_validator=_active_session,
        jwks_fetcher=fetch,
    )
    asyncio.run(adapter.verify_bearer(_token(private_key)))

    for key_id in ("attacker-key-1", "attacker-key-2"):
        with pytest.raises(MarketMorningAuthenticationRejected):
            asyncio.run(
                adapter.verify_bearer(_token(private_key, kid=key_id))
            )
    assert calls == ["fetch", "fetch"]


def test_provider_outage_is_not_misclassified_as_user_rejection() -> None:
    from src.market_morning.oidc_auth import (
        OidcProviderUnavailable,
        build_product_auth_adapter,
    )

    private_key = _key()

    async def unavailable():
        raise OidcProviderUnavailable("safe-provider-code")

    adapter = build_product_auth_adapter(
        settings=_settings(),
        session_validator=_active_session,
        jwks_fetcher=unavailable,
    )

    with pytest.raises(OidcProviderUnavailable):
        asyncio.run(adapter.verify_bearer(_token(private_key)))


def test_session_validator_runs_per_request_and_revocation_fails_closed() -> None:
    from src.api.market_morning_auth import MarketMorningAuthenticationRejected
    from src.market_morning.oidc_auth import build_product_auth_adapter

    private_key = _key()
    decisions = [True, False]
    calls = []

    async def fetch():
        return {"keys": [_jwk(private_key, kid="key-1")]}

    async def validate(token: str, claims) -> bool:
        calls.append((token, claims["sid"]))
        with pytest.raises(TypeError):
            claims["sid"] = "forged"
        return decisions[len(calls) - 1]

    token = _token(private_key)
    adapter = build_product_auth_adapter(
        settings=_settings(),
        session_validator=validate,
        jwks_fetcher=fetch,
    )

    asyncio.run(adapter.verify_bearer(token))
    with pytest.raises(MarketMorningAuthenticationRejected):
        asyncio.run(adapter.verify_bearer(token))
    assert calls == [(token, "provider-session-17")] * 2


def test_admin_roles_map_only_to_explicit_least_privilege_permissions() -> None:
    from src.api.market_morning_admin_auth import (
        MarketMorningAdminAuthenticationRejected,
    )
    from src.market_morning.oidc_auth import build_admin_auth_adapter

    private_key = _key()

    async def fetch():
        return {"keys": [_jwk(private_key, kid="key-1")]}

    adapter = build_admin_auth_adapter(
        settings=_settings(),
        session_validator=_active_session,
        jwks_fetcher=fetch,
        role_permissions={
            "morning-reader": frozenset({"operations.read"}),
            "morning-reviewer": frozenset({"content.review"}),
        },
    )
    allowed = asyncio.run(
        adapter.verify_bearer(
            _token(
                private_key,
                claims=_claims(
                    roles=["morning-reader", "morning-reviewer", "unknown"]
                ),
            )
        )
    )
    assert allowed.permissions == frozenset(
        {"operations.read", "content.review"}
    )
    assert SUBJECT not in allowed.actor_reference

    with pytest.raises(MarketMorningAdminAuthenticationRejected):
        asyncio.run(
            adapter.verify_bearer(
                _token(private_key, claims=_claims(roles=["unknown"]))
            )
        )


def test_role_mapping_and_endpoint_configuration_fail_closed() -> None:
    from src.market_morning.oidc_auth import (
        OidcConfigurationError,
        OidcSettings,
        parse_admin_role_permissions,
    )

    with pytest.raises(OidcConfigurationError):
        OidcSettings(
            provider="oidc",
            issuer="http://identity.example.com",
            jwks_url=JWKS_URL,
            audience=AUDIENCE,
        )
    with pytest.raises(OidcConfigurationError):
        OidcSettings(
            provider="oidc",
            issuer=ISSUER,
            jwks_url="https://127.0.0.1/jwks",
            audience=AUDIENCE,
        )
    with pytest.raises(OidcConfigurationError):
        parse_admin_role_permissions(
            json.dumps({"admin": ["root.everything"]})
        )


def test_builtin_factories_load_required_session_validator_and_role_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.market_morning_admin_auth import MarketMorningAdminAuthAdapter
    from src.api.market_morning_auth import MarketMorningProductAuthAdapter
    from src.config.accessor import reset_env_config
    from src.market_morning.oidc_auth import (
        build_admin_auth_adapter,
        build_product_auth_adapter,
    )

    async def active(_token: str, _claims) -> bool:
        return True

    monkeypatch.setitem(
        sys.modules,
        "test_market_morning_oidc_deployment",
        SimpleNamespace(build_session_validator=lambda: active),
    )
    values = {
        "VIBE_MARKET_MORNING_OIDC_PROVIDER": "deployment-oidc",
        "VIBE_MARKET_MORNING_OIDC_ISSUER": ISSUER,
        "VIBE_MARKET_MORNING_OIDC_JWKS_URL": JWKS_URL,
        "VIBE_MARKET_MORNING_OIDC_AUDIENCE": AUDIENCE,
        "VIBE_MARKET_MORNING_OIDC_SESSION_VALIDATOR_FACTORY": (
            "test_market_morning_oidc_deployment:build_session_validator"
        ),
        "VIBE_MARKET_MORNING_OIDC_ADMIN_ROLE_PERMISSIONS_JSON": json.dumps(
            {"operator": ["operations.read"]}
        ),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    reset_env_config()
    try:
        assert isinstance(build_product_auth_adapter(), MarketMorningProductAuthAdapter)
        assert isinstance(build_admin_auth_adapter(), MarketMorningAdminAuthAdapter)
    finally:
        reset_env_config()


def test_builtin_oidc_preflight_emits_only_stable_codes() -> None:
    from src.config.env_schema import MarketMorningConfig
    from src.market_morning.oidc_auth import (
        BUILTIN_ADMIN_AUTH_FACTORY,
        BUILTIN_PRODUCT_AUTH_FACTORY,
        builtin_oidc_preflight_checks,
    )

    missing = builtin_oidc_preflight_checks(
        MarketMorningConfig(
            auth_factory=BUILTIN_PRODUCT_AUTH_FACTORY,
            admin_auth_factory=BUILTIN_ADMIN_AUTH_FACTORY,
        )
    )
    assert missing == (
        "oidc_admin_role_permissions_missing",
        "oidc_audience_missing",
        "oidc_issuer_missing",
        "oidc_jwks_url_missing",
        "oidc_session_validator_factory_missing",
    )

    ready = builtin_oidc_preflight_checks(
        MarketMorningConfig(
            auth_factory=BUILTIN_PRODUCT_AUTH_FACTORY,
            admin_auth_factory=BUILTIN_ADMIN_AUTH_FACTORY,
            oidc_issuer=ISSUER,
            oidc_jwks_url=JWKS_URL,
            oidc_audience=AUDIENCE,
            oidc_session_validator_factory="deployment.identity:build_session",
            oidc_admin_role_permissions_json=json.dumps(
                {"operator": ["operations.read"]}
            ),
        )
    )
    assert ready == ()

    invalid_factory = builtin_oidc_preflight_checks(
        MarketMorningConfig(
            auth_factory=BUILTIN_PRODUCT_AUTH_FACTORY,
            oidc_issuer=ISSUER,
            oidc_jwks_url=JWKS_URL,
            oidc_audience=AUDIENCE,
            oidc_session_validator_factory="not-a-module-path",
        )
    )
    assert invalid_factory == ("oidc_session_validator_factory_invalid",)
