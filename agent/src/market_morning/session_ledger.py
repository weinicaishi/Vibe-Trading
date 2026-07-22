"""Application-owned, hash-only OIDC session ledger.

The identity provider remains responsible for authentication. This ledger adds
an immediate, local revocation boundary without persisting raw tokens, raw
provider subjects, or raw provider session identifiers.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert

from src.config.accessor import get_env_config
from src.market_morning.db import get_session_factory
from src.market_morning.models import (
    ApplicationSessionRecord,
    new_id,
    utc_now_naive,
)

BUILTIN_SESSION_VALIDATOR_FACTORY = "src.market_morning.session_ledger:build_session_validator"
_MAX_SESSION_REFERENCE_LENGTH = 512
_MAX_ACCESS_TOKEN_LENGTH = 8192
ACCESS_TOKEN_SESSION_REFERENCE = "__access_token_sha256__"


class SessionLedgerConfigurationError(RuntimeError):
    pass


def _safe_claim(value: Any, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or value != value.strip()
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise SessionLedgerConfigurationError("oidc_session_claim_invalid")
    return value


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def pseudonymous_oidc_subject_reference(issuer: str, subject: str) -> str:
    digest = hashlib.sha256(f"{issuer}\0{subject}".encode("utf-8")).hexdigest()
    return f"oidc:{digest}"


def _utc_naive_timestamp(value: Any) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SessionLedgerConfigurationError("oidc_session_claim_invalid")
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError) as error:
        raise SessionLedgerConfigurationError("oidc_session_claim_invalid") from error


def _identity(
    claims: Mapping[str, Any],
    *,
    claim_name: str,
    access_token: str | None = None,
) -> tuple[str, str, str, datetime, datetime]:
    issuer = _safe_claim(claims.get("iss"), maximum=2048)
    subject = _safe_claim(claims.get("sub"), maximum=255)
    if claim_name == ACCESS_TOKEN_SESSION_REFERENCE:
        session_reference = _safe_claim(access_token, maximum=_MAX_ACCESS_TOKEN_LENGTH)
    else:
        session_reference = _safe_claim(claims.get(claim_name), maximum=_MAX_SESSION_REFERENCE_LENGTH)
    return (
        _sha256(issuer),
        _sha256(session_reference),
        pseudonymous_oidc_subject_reference(issuer, subject),
        _utc_naive_timestamp(claims.get("iat")),
        _utc_naive_timestamp(claims.get("exp")),
    )


async def revoke_all_sessions_for_subject(
    session,
    *,
    external_subject: str,
    reason: str,
    now: datetime | None = None,
) -> int:
    occurred_at = now or utc_now_naive()
    result = await session.execute(
        update(ApplicationSessionRecord)
        .where(
            ApplicationSessionRecord.external_subject == external_subject,
            ApplicationSessionRecord.status == "active",
        )
        .values(
            status="revoked",
            revoked_at=occurred_at,
            revocation_reason=reason,
            updated_at=occurred_at,
        )
    )
    return int(result.rowcount or 0)


async def revoke_and_pseudonymize_sessions_for_subject(
    session,
    *,
    external_subject: str,
    replacement_subject: str,
    reason: str,
    now: datetime | None = None,
) -> int:
    occurred_at = now or utc_now_naive()
    replacement = _safe_claim(replacement_subject, maximum=255)
    result = await session.execute(
        update(ApplicationSessionRecord)
        .where(ApplicationSessionRecord.external_subject == external_subject)
        .values(
            external_subject=replacement,
            status="revoked",
            revoked_at=occurred_at,
            revocation_reason=reason,
            updated_at=occurred_at,
        )
    )
    return int(result.rowcount or 0)


class ApplicationSessionValidator:
    def __init__(self, *, claim_name: str, session_factory=None) -> None:
        self.claim_name = _safe_claim(claim_name, maximum=128)
        self.session_factory = session_factory or get_session_factory()

    async def __call__(self, _token: str, claims: Mapping[str, Any]) -> bool:
        issuer_hash, session_hash, external_subject, issued_at, expires_at = _identity(
            claims,
            claim_name=self.claim_name,
            access_token=_token,
        )
        now = utc_now_naive()
        statement = mysql_insert(ApplicationSessionRecord).values(
            auth_session_id=new_id(),
            issuer_sha256=issuer_hash,
            session_reference_sha256=session_hash,
            external_subject=external_subject,
            status="active",
            token_issued_at=issued_at,
            token_expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        statement = statement.on_duplicate_key_update(auth_session_id=ApplicationSessionRecord.auth_session_id)
        async with self.session_factory.begin() as session:
            await session.execute(statement)
            row = (
                await session.execute(
                    select(ApplicationSessionRecord).where(
                        ApplicationSessionRecord.issuer_sha256 == issuer_hash,
                        ApplicationSessionRecord.session_reference_sha256 == session_hash,
                    )
                )
            ).scalar_one()
        return row.status == "active" and row.external_subject == external_subject

    async def revoke_claims(
        self,
        claims: Mapping[str, Any],
        *,
        reason: str = "logout",
    ) -> bool:
        issuer_hash, session_hash, external_subject, _, _ = _identity(claims, claim_name=self.claim_name)
        now = utc_now_naive()
        async with self.session_factory.begin() as session:
            result = await session.execute(
                update(ApplicationSessionRecord)
                .where(
                    ApplicationSessionRecord.issuer_sha256 == issuer_hash,
                    ApplicationSessionRecord.session_reference_sha256 == session_hash,
                    ApplicationSessionRecord.external_subject == external_subject,
                    ApplicationSessionRecord.status == "active",
                )
                .values(
                    status="revoked",
                    revoked_at=now,
                    revocation_reason=reason,
                    updated_at=now,
                )
            )
        return int(result.rowcount or 0) == 1

    async def revoke_token(
        self,
        token: str,
        claims: Mapping[str, Any],
        *,
        reason: str = "logout",
    ) -> bool:
        if self.claim_name != ACCESS_TOKEN_SESSION_REFERENCE:
            return await self.revoke_claims(claims, reason=reason)
        issuer_hash, session_hash, external_subject, _, _ = _identity(
            claims,
            claim_name=self.claim_name,
            access_token=token,
        )
        now = utc_now_naive()
        async with self.session_factory.begin() as session:
            result = await session.execute(
                update(ApplicationSessionRecord)
                .where(
                    ApplicationSessionRecord.issuer_sha256 == issuer_hash,
                    ApplicationSessionRecord.session_reference_sha256 == session_hash,
                    ApplicationSessionRecord.external_subject == external_subject,
                    ApplicationSessionRecord.status == "active",
                )
                .values(
                    status="revoked",
                    revoked_at=now,
                    revocation_reason=reason,
                    updated_at=now,
                )
            )
        return int(result.rowcount or 0) == 1


def build_session_validator() -> ApplicationSessionValidator:
    config = get_env_config().market_morning
    return ApplicationSessionValidator(claim_name=config.oidc_session_claim)


__all__ = [
    "ACCESS_TOKEN_SESSION_REFERENCE",
    "ApplicationSessionValidator",
    "BUILTIN_SESSION_VALIDATOR_FACTORY",
    "SessionLedgerConfigurationError",
    "build_session_validator",
    "pseudonymous_oidc_subject_reference",
    "revoke_and_pseudonymize_sessions_for_subject",
    "revoke_all_sessions_for_subject",
]
