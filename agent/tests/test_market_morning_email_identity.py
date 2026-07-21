"""Contract tests for deployment-owned verified email identity lookup."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import sys

import pytest

USER_ID = "11111111-1111-4111-8111-111111111111"
OTHER_USER_ID = "22222222-2222-4222-8222-222222222222"
SUBJECT = "oidc:" + "a" * 64
EMAIL = "private.user@Example.JP"


def _identity(
    *,
    user_id: str = USER_ID,
    external_subject: str = SUBJECT,
    email: str | None = EMAIL,
    email_verified: bool = True,
    active: bool = True,
):
    from src.market_morning.email_identity import VerifiedEmailIdentity

    return VerifiedEmailIdentity(
        user_id=user_id,
        external_subject=external_subject,
        email=email,
        email_verified=email_verified,
        active=active,
    )


def _install_adapter(monkeypatch: pytest.MonkeyPatch, resolver, *, name: str):
    from src.market_morning.email_identity import (
        MarketMorningEmailIdentityAdapter,
        clear_email_identity_adapter_cache,
    )

    module_name = f"test_market_morning_email_identity_{name}"
    adapter = MarketMorningEmailIdentityAdapter(
        provider="test-directory",
        resolve_identity=resolver,
    )
    monkeypatch.setitem(
        sys.modules,
        module_name,
        SimpleNamespace(build=lambda: adapter),
    )
    clear_email_identity_adapter_cache()
    return f"{module_name}:build", adapter


def test_identity_resolver_returns_only_verified_active_exact_user_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.market_morning.email_identity import build_email_identity_resolver

    calls = []

    async def directory(user_id: str, external_subject: str):
        calls.append((user_id, external_subject))
        return _identity()

    factory_path, adapter = _install_adapter(
        monkeypatch,
        directory,
        name="success",
    )
    resolver = build_email_identity_resolver(factory_path)

    destination = asyncio.run(resolver(USER_ID, SUBJECT))

    assert destination == "private.user@example.jp"
    assert calls == [(USER_ID, SUBJECT)]
    assert EMAIL not in repr(adapter)
    assert EMAIL not in repr(_identity())


@pytest.mark.parametrize(
    "identity",
    [
        None,
        _identity(email_verified=False),
        _identity(active=False),
        _identity(email=None, email_verified=False),
    ],
)
def test_identity_resolver_rejects_missing_inactive_or_unverified_destination(
    monkeypatch: pytest.MonkeyPatch,
    identity,
) -> None:
    from src.market_morning.email_identity import build_email_identity_resolver

    async def directory(_user_id: str, _external_subject: str):
        return identity

    factory_path, _adapter = _install_adapter(
        monkeypatch,
        directory,
        name=f"ineligible_{id(identity)}",
    )
    resolver = build_email_identity_resolver(factory_path)

    with pytest.raises(ValueError, match="unavailable"):
        asyncio.run(resolver(USER_ID, SUBJECT))


@pytest.mark.parametrize(
    "identity",
    [
        _identity(user_id=OTHER_USER_ID),
        _identity(external_subject="oidc:" + "b" * 64),
        "private.user@example.jp",
    ],
)
def test_identity_resolver_fails_closed_on_wrong_user_or_adapter_contract(
    monkeypatch: pytest.MonkeyPatch,
    identity,
) -> None:
    from src.market_morning.email_delivery_service import (
        EmailDeliveryPortUnavailable,
    )
    from src.market_morning.email_identity import build_email_identity_resolver

    async def directory(_user_id: str, _external_subject: str):
        return identity

    factory_path, _adapter = _install_adapter(
        monkeypatch,
        directory,
        name=f"mismatch_{id(identity)}",
    )
    resolver = build_email_identity_resolver(factory_path)

    with pytest.raises(EmailDeliveryPortUnavailable) as caught:
        asyncio.run(resolver(USER_ID, SUBJECT))
    assert str(caught.value) == "email_identity_unavailable"
    assert USER_ID not in str(caught.value)
    assert EMAIL not in str(caught.value)


def test_identity_resolver_sanitizes_directory_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.market_morning.email_delivery_service import (
        EmailDeliveryPortUnavailable,
    )
    from src.market_morning.email_identity import (
        EmailIdentityDirectoryUnavailable,
        build_email_identity_resolver,
    )

    for name, failure in (
        (
            "known_failure",
            EmailIdentityDirectoryUnavailable(f"directory failed for {EMAIL}"),
        ),
        ("unknown_failure", RuntimeError(f"provider payload contains {EMAIL}")),
    ):
        async def directory(_user_id: str, _external_subject: str, *, error=failure):
            raise error

        factory_path, _adapter = _install_adapter(
            monkeypatch,
            directory,
            name=name,
        )
        resolver = build_email_identity_resolver(factory_path)
        with pytest.raises(EmailDeliveryPortUnavailable) as caught:
            asyncio.run(resolver(USER_ID, SUBJECT))
        assert str(caught.value) == "email_identity_unavailable"
        assert EMAIL not in str(caught.value)


@pytest.mark.parametrize(
    "email",
    [
        "missing-at.example.jp",
        "two@@example.jp",
        "space user@example.jp",
        ".leading@example.jp",
        "double..dot@example.jp",
        "user@localhost",
        "user@-example.jp",
        "user@example..jp",
        "user@example.jp\nBcc: attacker@example.jp",
    ],
)
def test_verified_identity_rejects_unsafe_email(email: str) -> None:
    with pytest.raises(ValueError):
        _identity(email=email)


def test_factory_loader_is_cached_and_validates_exact_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.market_morning.email_identity import (
        EmailIdentityConfigurationError,
        MarketMorningEmailIdentityAdapter,
        build_email_identity_resolver,
        clear_email_identity_adapter_cache,
        load_email_identity_adapter,
    )

    calls = []

    async def directory(_user_id: str, _external_subject: str):
        return _identity()

    adapter = MarketMorningEmailIdentityAdapter(
        provider="test-directory",
        resolve_identity=directory,
    )
    monkeypatch.setitem(
        sys.modules,
        "test_market_morning_email_identity_cache",
        SimpleNamespace(build=lambda: calls.append("build") or adapter),
    )
    path = "test_market_morning_email_identity_cache:build"
    clear_email_identity_adapter_cache()

    build_email_identity_resolver(path)
    build_email_identity_resolver(path)

    assert calls == ["build"]
    assert load_email_identity_adapter(path) is adapter
    assert calls == ["build", "build"]

    for invalid_path in ("", "module", "module:function:extra", "module :function"):
        with pytest.raises(EmailIdentityConfigurationError) as caught:
            load_email_identity_adapter(invalid_path)
        assert caught.value.error_code == "email_identity_factory_invalid"

    monkeypatch.setitem(
        sys.modules,
        "test_market_morning_email_identity_bad",
        SimpleNamespace(build=lambda: object()),
    )
    with pytest.raises(EmailIdentityConfigurationError) as caught:
        load_email_identity_adapter("test_market_morning_email_identity_bad:build")
    assert caught.value.error_code == "email_identity_factory_contract_invalid"


def test_email_identity_static_preflight_uses_stable_non_secret_codes() -> None:
    from src.market_morning.email_identity import (
        email_identity_static_preflight_checks,
    )

    assert email_identity_static_preflight_checks("") == (
        "email_identity_factory_missing",
    )
    assert email_identity_static_preflight_checks("not-a-factory") == (
        "email_identity_factory_invalid",
    )
    assert email_identity_static_preflight_checks(
        "deployment.identity:build_email_identity"
    ) == ()
