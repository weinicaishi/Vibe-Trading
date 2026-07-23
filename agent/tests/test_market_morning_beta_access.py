"""Private-beta invitation and account-access lifecycle tests."""

from __future__ import annotations

import asyncio
import hashlib
import sys
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql

from src.market_morning.beta_access import (
    BetaAccessConflict,
    BetaInviteStatus,
    UserAccessMutationStatus,
    accept_private_beta_invite,
    create_private_beta_invite,
    revoke_private_beta_invite,
    set_user_access,
)
from src.market_morning.models import AuditLog, Base, PrivateBetaInvite, User


NOW = datetime(2026, 7, 21, 1, 0, 0)
USER_ID = "11111111-1111-4111-8111-111111111111"
INVITE_ID = "22222222-2222-4222-8222-222222222222"
RAW_TOKEN = "a" * 43


class _Result:
    def __init__(self, *, scalar=None, rowcount=0):
        self.scalar = scalar
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self.scalar


class _Session:
    def __init__(self, *results):
        self.results = deque(results)
        self.statements = []
        self.added = []
        self.flush_count = 0
        self.flush_snapshots = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.popleft()

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flush_count += 1
        self.flush_snapshots.append(tuple(type(value) for value in self.added))


def _mysql_ddl(table_name: str) -> str:
    from sqlalchemy.schema import CreateTable

    return str(CreateTable(Base.metadata.tables[table_name]).compile(dialect=mysql.dialect()))


def _invite(*, status: str = "pending", expires_at: datetime | None = None):
    return PrivateBetaInvite(
        invite_id=INVITE_ID,
        token_sha256=hashlib.sha256(RAW_TOKEN.encode()).hexdigest(),
        status=status,
        expires_at=expires_at or NOW + timedelta(days=7),
        created_by_reference="vibe-api-key-operator",
        created_at=NOW,
        updated_at=NOW,
    )


def _user(*, status: str = "active"):
    return SimpleNamespace(
        user_id=USER_ID,
        external_subject="oidc|subject",
        account_status=status,
        trial_or_subscription_status="private_beta",
        email_opt_in=True,
        last_product_activity_at=NOW,
        deleted_at=None,
        updated_at=NOW,
    )


def test_invite_schema_stores_hash_only_and_has_bounded_statuses() -> None:
    table = Base.metadata.tables["mm_private_beta_invites"]
    ddl = _mysql_ddl("mm_private_beta_invites")

    assert "token_sha256" in table.c
    assert "token" not in table.c
    assert "UNIQUE (token_sha256)" in ddl
    assert "pending" in ddl and "accepted" in ddl and "revoked" in ddl
    assert "expired" in ddl

    migration = Path("agent/migrations/market_morning/versions/0015_market_morning_beta_privacy.py").read_text()
    assert 'down_revision: str | None = "0014_market_morning_issuer_research"' in migration
    assert '"mm_private_beta_invites"' in migration


def test_create_invite_returns_secret_once_but_persists_and_audits_hash_only() -> None:
    session = _Session()

    result = asyncio.run(
        create_private_beta_invite(
            session,
            actor_reference="vibe-api-key-operator",
            expires_at=NOW + timedelta(days=7),
            now=NOW,
            token_factory=lambda: RAW_TOKEN,
        )
    )

    invite = next(value for value in session.added if isinstance(value, PrivateBetaInvite))
    audit = next(value for value in session.added if isinstance(value, AuditLog))
    assert result.raw_token == RAW_TOKEN
    assert invite.token_sha256 == hashlib.sha256(RAW_TOKEN.encode()).hexdigest()
    assert RAW_TOKEN not in repr(invite.__dict__)
    assert RAW_TOKEN not in repr(audit.details)
    assert audit.details == {
        "actor_reference": "vibe-api-key-operator",
        "expires_at": (NOW + timedelta(days=7)).isoformat(),
    }
    assert session.flush_count == 1


def test_accept_invite_activates_one_private_beta_user_and_is_audited() -> None:
    invite = _invite()
    session = _Session(_Result(scalar=invite), _Result(scalar=None))

    result = asyncio.run(
        accept_private_beta_invite(
            session,
            raw_token=RAW_TOKEN,
            external_subject="oidc|subject",
            now=NOW,
        )
    )

    user = next(value for value in session.added if isinstance(value, User))
    audit = next(value for value in session.added if isinstance(value, AuditLog))
    assert result.status == BetaInviteStatus.ACCEPTED
    assert result.user_id == user.user_id
    assert user.account_status == "active"
    assert user.trial_or_subscription_status == "private_beta"
    assert invite.accepted_by_user_id == user.user_id
    assert invite.accepted_at == NOW
    assert invite.status == "accepted"
    assert audit.actor_user_id == user.user_id
    assert RAW_TOKEN not in repr(audit.details)
    assert session.flush_count == 2
    assert User in session.flush_snapshots[0]
    assert AuditLog not in session.flush_snapshots[0]
    assert AuditLog in session.flush_snapshots[1]


def test_accept_invite_expires_fail_closed_without_creating_user() -> None:
    invite = _invite(expires_at=NOW - timedelta(seconds=1))
    session = _Session(_Result(scalar=invite))

    with pytest.raises(BetaAccessConflict, match="unavailable"):
        asyncio.run(
            accept_private_beta_invite(
                session,
                raw_token=RAW_TOKEN,
                external_subject="oidc|subject",
                now=NOW,
            )
        )

    assert invite.status == "expired"
    assert not any(isinstance(value, User) for value in session.added)


def test_revoke_invite_and_suspend_user_are_idempotent_and_audited() -> None:
    invite = _invite()
    revoke_session = _Session(_Result(scalar=invite))
    revoked = asyncio.run(
        revoke_private_beta_invite(
            revoke_session,
            invite_id=INVITE_ID,
            actor_reference="vibe-api-key-operator",
            now=NOW,
        )
    )
    assert revoked.status == BetaInviteStatus.REVOKED
    assert invite.revoked_at == NOW

    user = _user()
    suspend_session = _Session(_Result(scalar=user), _Result(rowcount=2))
    suspended = asyncio.run(
        set_user_access(
            suspend_session,
            user_id=USER_ID,
            action="suspend",
            reason_code="beta_access_revoked",
            actor_reference="vibe-api-key-operator",
            now=NOW,
        )
    )
    assert suspended.status == UserAccessMutationStatus.SUSPENDED
    assert user.account_status == "suspended"
    assert user.email_opt_in is False
    assert user.last_product_activity_at is None

    repeat_session = _Session(_Result(scalar=user), _Result(rowcount=0))
    repeated = asyncio.run(
        set_user_access(
            repeat_session,
            user_id=USER_ID,
            action="suspend",
            reason_code="beta_access_revoked",
            actor_reference="vibe-api-key-operator",
            now=NOW,
        )
    )
    assert repeated.status == UserAccessMutationStatus.ALREADY_SUSPENDED
    assert repeat_session.added == []


def test_user_reactivation_cannot_promote_an_unaccepted_invited_account() -> None:
    invited = _user(status="invited")
    session = _Session(_Result(scalar=invited))

    with pytest.raises(BetaAccessConflict, match="transition"):
        asyncio.run(
            set_user_access(
                session,
                user_id=USER_ID,
                action="reactivate",
                reason_code="support_resolution",
                actor_reference="vibe-api-key-operator",
                now=NOW,
            )
        )

    assert invited.account_status == "invited"


def test_admin_invite_and_user_access_routes_use_fixed_operator_identity(
    monkeypatch,
) -> None:
    from src.api import market_morning_admin_routes
    from src.market_morning.beta_access import (
        BetaInviteCreationResult,
        BetaInviteMutationResult,
        UserAccessMutationResult,
    )

    async def allow_request() -> None:
        return None

    monkeypatch.setitem(
        sys.modules,
        "api_server",
        SimpleNamespace(require_auth=allow_request),
    )
    monkeypatch.setenv("VIBE_MARKET_MORNING_ENABLED", "true")
    calls = []

    async def create(**kwargs):
        calls.append(("create", kwargs))
        return BetaInviteCreationResult(
            status=BetaInviteStatus.CREATED,
            invite_id=INVITE_ID,
            raw_token=RAW_TOKEN,
            expires_at=NOW + timedelta(days=7),
        )

    async def revoke(**kwargs):
        calls.append(("revoke", kwargs))
        return BetaInviteMutationResult(
            status=BetaInviteStatus.REVOKED,
            invite_id=INVITE_ID,
            user_id=None,
            expires_at=NOW + timedelta(days=7),
        )

    async def access(**kwargs):
        calls.append(("access", kwargs))
        return UserAccessMutationResult(
            status=UserAccessMutationStatus.SUSPENDED,
            user_id=USER_ID,
            account_status="suspended",
        )

    monkeypatch.setattr(market_morning_admin_routes, "create_invite", create)
    monkeypatch.setattr(market_morning_admin_routes, "revoke_invite", revoke)
    monkeypatch.setattr(market_morning_admin_routes, "mutate_user_access", access)
    app = FastAPI()
    market_morning_admin_routes.register_market_morning_admin_routes(app)
    client = TestClient(app, client=("127.0.0.1", 50000))

    created = client.post(
        "/market-morning/_internal/private-beta/invites",
        json={"expires_in_days": 7},
    )
    revoked = client.delete(f"/market-morning/_internal/private-beta/invites/{INVITE_ID}")
    suspended = client.post(
        f"/market-morning/_internal/users/{USER_ID}/access",
        json={"action": "suspend", "reason_code": "beta_access_revoked"},
    )

    assert created.status_code == 200
    assert created.json()["raw_token"] == RAW_TOKEN
    assert revoked.status_code == 200
    assert suspended.status_code == 200
    assert calls[0][1]["actor_reference"] == "vibe-api-key-operator"
    assert calls[1][1]["actor_reference"] == "vibe-api-key-operator"
    assert calls[2][1]["actor_reference"] == "vibe-api-key-operator"
    assert RAW_TOKEN not in repr(calls)


def test_invite_acceptance_route_uses_verified_oidc_subject_not_client_identity(
    monkeypatch,
) -> None:
    from src.api import market_morning_routes
    from src.api.market_morning_auth import (
        MarketMorningOnboardingPrincipal,
        require_market_morning_onboarding_principal,
    )
    from src.market_morning.beta_access import BetaInviteMutationResult

    async def allow_internal_request() -> None:
        return None

    async def onboarding_principal() -> MarketMorningOnboardingPrincipal:
        return MarketMorningOnboardingPrincipal(external_subject="oidc|verified")

    monkeypatch.setitem(
        sys.modules,
        "api_server",
        SimpleNamespace(require_auth=allow_internal_request),
    )
    calls = []

    async def accept(**kwargs):
        calls.append(kwargs)
        return BetaInviteMutationResult(
            status=BetaInviteStatus.ACCEPTED,
            invite_id=INVITE_ID,
            user_id=USER_ID,
            expires_at=NOW + timedelta(days=7),
        )

    monkeypatch.setattr(market_morning_routes, "accept_invite", accept)
    app = FastAPI()
    market_morning_routes.register_market_morning_routes(app)
    app.dependency_overrides[require_market_morning_onboarding_principal] = onboarding_principal
    client = TestClient(app, client=("127.0.0.1", 50000))

    response = client.post(
        "/market-morning/private-beta/invitations/accept",
        json={"token": RAW_TOKEN},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert calls == [{"raw_token": RAW_TOKEN, "external_subject": "oidc|verified"}]
