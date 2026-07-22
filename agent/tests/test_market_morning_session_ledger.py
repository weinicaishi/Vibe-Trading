"""Hash-only OIDC session ledger contracts."""

from __future__ import annotations

import asyncio
import hashlib
from collections import deque
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.dialects import mysql

from src.market_morning.models import Base
from src.market_morning.session_ledger import (
    ApplicationSessionValidator,
    pseudonymous_oidc_subject_reference,
    revoke_and_pseudonymize_sessions_for_subject,
    revoke_all_sessions_for_subject,
)

ISSUER = "https://identity.example.com/market-morning"
SUBJECT = "provider-subject-17"
SESSION = "provider-session-17"


class _Result:
    def __init__(self, *, scalar=None, rowcount=0):
        self._scalar = scalar
        self.rowcount = rowcount

    def scalar_one(self):
        return self._scalar


class _Session:
    def __init__(self, *results):
        self.results = deque(results)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.popleft()


class _Begin:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return False


class _Factory:
    def __init__(self, session):
        self.session = session

    def begin(self):
        return _Begin(self.session)


def _claims():
    return {
        "iss": ISSUER,
        "sub": SUBJECT,
        "sid": SESSION,
        "iat": 1_768_435_200,
        "exp": 1_768_436_100,
    }


def test_session_schema_is_hash_only_and_revocation_state_is_bounded() -> None:
    from sqlalchemy.schema import CreateTable

    table = Base.metadata.tables["mm_auth_sessions"]
    ddl = str(CreateTable(table).compile(dialect=mysql.dialect()))

    assert "issuer_sha256" in table.c
    assert "session_reference_sha256" in table.c
    assert "token" not in table.c
    assert "raw_subject" not in table.c
    assert "UNIQUE (issuer_sha256, session_reference_sha256)" in ddl
    assert "active" in ddl and "revoked" in ddl

    migration = Path("agent/migrations/market_morning/versions/0018_market_morning_auth_sessions.py").read_text(
        encoding="utf-8"
    )
    assert 'down_revision: str | None = "0017_market_morning_content_reports"' in migration
    assert 'revision: str = "0018_market_morning_auth_sessions"' in migration
    assert '"mm_auth_sessions"' in migration


def test_first_valid_token_is_provisioned_without_persisting_raw_identifiers() -> None:
    row = SimpleNamespace(
        status="active",
        external_subject=pseudonymous_oidc_subject_reference(ISSUER, SUBJECT),
    )
    session = _Session(_Result(), _Result(scalar=row))
    validator = ApplicationSessionValidator(claim_name="sid", session_factory=_Factory(session))

    assert asyncio.run(validator("raw.jwt.token", _claims())) is True
    insert = session.statements[0].compile(dialect=mysql.dialect())
    rendered = repr(insert.params)
    assert hashlib.sha256(ISSUER.encode()).hexdigest() in rendered
    assert hashlib.sha256(SESSION.encode()).hexdigest() in rendered
    assert ISSUER not in rendered
    assert SUBJECT not in rendered
    assert SESSION not in rendered
    assert "raw.jwt.token" not in rendered


def test_token_instance_mode_hashes_access_token_without_requiring_session_claim() -> None:
    row = SimpleNamespace(
        status="active",
        external_subject=pseudonymous_oidc_subject_reference(ISSUER, SUBJECT),
    )
    session = _Session(_Result(), _Result(scalar=row))
    validator = ApplicationSessionValidator(
        claim_name="__access_token_sha256__",
        session_factory=_Factory(session),
    )
    claims = {key: value for key, value in _claims().items() if key != "sid"}

    assert asyncio.run(validator("raw.jwt.token", claims)) is True

    rendered = repr(session.statements[0].compile(dialect=mysql.dialect()).params)
    assert hashlib.sha256(b"raw.jwt.token").hexdigest() in rendered
    assert "raw.jwt.token" not in rendered


def test_revoked_or_cross_subject_session_is_rejected_and_never_reactivated() -> None:
    for row in (
        SimpleNamespace(
            status="revoked",
            external_subject=pseudonymous_oidc_subject_reference(ISSUER, SUBJECT),
        ),
        SimpleNamespace(status="active", external_subject="oidc:another-subject"),
    ):
        session = _Session(_Result(), _Result(scalar=row))
        validator = ApplicationSessionValidator(claim_name="sid", session_factory=_Factory(session))
        assert asyncio.run(validator("raw.jwt.token", _claims())) is False
        sql = str(session.statements[0].compile(dialect=mysql.dialect()))
        assert "ON DUPLICATE KEY UPDATE auth_session_id = mm_auth_sessions.auth_session_id" in sql


def test_subject_wide_revocation_is_idempotent() -> None:
    session = _Session(_Result(rowcount=3))
    count = asyncio.run(
        revoke_all_sessions_for_subject(
            session,
            external_subject="oidc:subject-hash",
            reason="account_suspended",
            now=datetime(2026, 7, 22, 1, 0, 0),
        )
    )
    assert count == 3
    params = session.statements[0].compile(dialect=mysql.dialect()).params
    assert "revoked" in params.values()
    assert "account_suspended" in params.values()


def test_account_deletion_pseudonymizes_every_historical_session() -> None:
    session = _Session(_Result(rowcount=4))
    count = asyncio.run(
        revoke_and_pseudonymize_sessions_for_subject(
            session,
            external_subject="oidc:subject-hash",
            replacement_subject="deleted-session:pseudonym",
            reason="account_deleted",
            now=datetime(2026, 7, 22, 1, 0, 0),
        )
    )
    assert count == 4
    params = session.statements[0].compile(dialect=mysql.dialect()).params
    assert "deleted-session:pseudonym" in params.values()
    assert "oidc:subject-hash" in params.values()
    assert "account_deleted" in params.values()
