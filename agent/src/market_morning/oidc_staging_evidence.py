"""Strict, privacy-safe evidence contract for real Auth0 staging flows."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
import re
from typing import Any
from zoneinfo import ZoneInfo


REQUIRED_OIDC_STAGING_CHECKS = frozenset(
    {
        "operator_exact_token_logout",
        "operator_login",
        "operator_protected_api",
        "operator_revoked_token_rejected",
        "operator_role_authorized",
        "product_exact_token_logout",
        "product_login",
        "product_private_beta_onboarding",
        "product_revoked_token_rejected",
        "product_watchlist_cross_session",
    }
)

_EXPECTED_FIELDS = frozenset(
    {
        "schema_version",
        "scope",
        "environment_tier",
        "run_id",
        "release_revision",
        "edition_date",
        "started_at",
        "finished_at",
        "identity_provider",
        "tenant_reference_sha256",
        "product_client_reference_sha256",
        "operator_client_reference_sha256",
        "api_audience_reference_sha256",
        "session_mode",
        "configured_max_access_token_lifetime_seconds",
        "observed_product_access_token_lifetime_seconds",
        "observed_operator_access_token_lifetime_seconds",
        "status",
        "counts_as_staging_oidc_evidence",
        "contains_access_token",
        "contains_authorization_header",
        "contains_id_token",
        "contains_invite_token",
        "contains_oauth_code_or_verifier",
        "contains_personal_identity",
        "contains_refresh_token",
        "checks",
        "failure_codes",
        "artifact_sha256",
    }
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_FAILURE_CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
_MAX_ACCESS_TOKEN_LIFETIME_SECONDS = 900
_JST = ZoneInfo("Asia/Tokyo")


class OidcStagingEvidenceError(ValueError):
    """Raised when OIDC evidence cannot count toward a staging day."""


def _aware_datetime(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise OidcStagingEvidenceError(f"{field_name} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise OidcStagingEvidenceError(
            f"{field_name} must be an ISO datetime"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OidcStagingEvidenceError(f"{field_name} must be timezone-aware")
    return parsed


def _iso_date(value: Any, *, field_name: str) -> date:
    if not isinstance(value, str):
        raise OidcStagingEvidenceError(f"{field_name} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise OidcStagingEvidenceError(f"{field_name} must be an ISO date") from error
    if parsed.isoformat() != value:
        raise OidcStagingEvidenceError(f"{field_name} must be an ISO date")
    return parsed


def _sha256(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise OidcStagingEvidenceError(
            f"{field_name} must be a lowercase SHA-256 digest"
        )
    return value


def _checks(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise OidcStagingEvidenceError("checks must be an object")
    if frozenset(value) != REQUIRED_OIDC_STAGING_CHECKS:
        raise OidcStagingEvidenceError(
            "checks must contain every required OIDC staging check"
        )
    result: dict[str, str] = {}
    for name in sorted(REQUIRED_OIDC_STAGING_CHECKS):
        status = value[name]
        if status not in {"passed", "failed"}:
            raise OidcStagingEvidenceError(
                "checks values must be passed or failed"
            )
        result[name] = status
    return result


def _artifact_hashes(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise OidcStagingEvidenceError("artifact_sha256 must be an object")
    if frozenset(value) != REQUIRED_OIDC_STAGING_CHECKS:
        raise OidcStagingEvidenceError(
            "artifact_sha256 must contain every required OIDC staging check"
        )
    return {
        name: _sha256(value[name], field_name="artifact_sha256")
        for name in sorted(REQUIRED_OIDC_STAGING_CHECKS)
    }


def _failure_codes(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) != len(set(value)):
        raise OidcStagingEvidenceError("failure_codes must be a unique list")
    result: list[str] = []
    for code in value:
        if not isinstance(code, str) or not _FAILURE_CODE_PATTERN.fullmatch(code):
            raise OidcStagingEvidenceError(
                "failure_codes must contain stable lowercase codes"
            )
        result.append(code)
    return tuple(result)


def _lifetime(value: Any, *, field_name: str, allow_zero: bool) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or not minimum <= value <= _MAX_ACCESS_TOKEN_LIFETIME_SECONDS:
        raise OidcStagingEvidenceError(
            f"{field_name} must be between {minimum} and 900 seconds"
        )
    return value


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class OidcStagingEvidence:
    run_id: str
    release_revision: str
    edition_date: date
    started_at: datetime
    finished_at: datetime
    identity_provider: str
    tenant_reference_sha256: str
    product_client_reference_sha256: str
    operator_client_reference_sha256: str
    api_audience_reference_sha256: str
    session_mode: str
    configured_max_access_token_lifetime_seconds: int
    observed_product_access_token_lifetime_seconds: int
    observed_operator_access_token_lifetime_seconds: int
    status: str
    counts_as_staging_oidc_evidence: bool
    checks: Mapping[str, str]
    failure_codes: tuple[str, ...]
    artifact_sha256: Mapping[str, str]
    evidence_sha256: str


def parse_oidc_staging_evidence(
    payload: Mapping[str, Any],
) -> OidcStagingEvidence:
    """Validate one real Product and Operator Auth0 staging exercise."""

    if not isinstance(payload, Mapping):
        raise OidcStagingEvidenceError("OIDC staging evidence must be an object")
    fields = frozenset(payload)
    if fields - _EXPECTED_FIELDS:
        raise OidcStagingEvidenceError(
            "OIDC staging evidence contains unexpected fields"
        )
    if _EXPECTED_FIELDS - fields:
        raise OidcStagingEvidenceError(
            "OIDC staging evidence is missing required fields"
        )
    if payload["schema_version"] != 1:
        raise OidcStagingEvidenceError("schema_version is unsupported")
    if payload["scope"] != "market_morning_oidc_staging_probe":
        raise OidcStagingEvidenceError("scope is not an OIDC staging probe")
    if payload["environment_tier"] != "staging":
        raise OidcStagingEvidenceError("environment_tier must be staging")

    run_id = payload["run_id"]
    if not isinstance(run_id, str) or not _RUN_ID_PATTERN.fullmatch(run_id):
        raise OidcStagingEvidenceError("run_id contains unsupported characters")
    release = payload["release_revision"]
    if not isinstance(release, str) or not _RELEASE_PATTERN.fullmatch(release):
        raise OidcStagingEvidenceError(
            "release_revision must be a lowercase immutable revision"
        )
    edition_date = _iso_date(payload["edition_date"], field_name="edition_date")
    started_at = _aware_datetime(payload["started_at"], field_name="started_at")
    finished_at = _aware_datetime(payload["finished_at"], field_name="finished_at")
    if not started_at < finished_at:
        raise OidcStagingEvidenceError("finished_at must follow started_at")
    if finished_at.astimezone(started_at.tzinfo) - started_at > timedelta(hours=2):
        raise OidcStagingEvidenceError(
            "OIDC staging exercise must finish within two hours"
        )
    if started_at.astimezone(_JST).date() != edition_date:
        raise OidcStagingEvidenceError("started_at must occur on edition_date in JST")
    if finished_at.astimezone(_JST).date() != edition_date:
        raise OidcStagingEvidenceError("finished_at must occur on edition_date in JST")

    provider = payload["identity_provider"]
    if provider != "auth0":
        raise OidcStagingEvidenceError("identity_provider must be auth0")
    tenant_reference = _sha256(
        payload["tenant_reference_sha256"],
        field_name="tenant_reference_sha256",
    )
    product_client_reference = _sha256(
        payload["product_client_reference_sha256"],
        field_name="product_client_reference_sha256",
    )
    operator_client_reference = _sha256(
        payload["operator_client_reference_sha256"],
        field_name="operator_client_reference_sha256",
    )
    audience_reference = _sha256(
        payload["api_audience_reference_sha256"],
        field_name="api_audience_reference_sha256",
    )
    if product_client_reference == operator_client_reference:
        raise OidcStagingEvidenceError(
            "product and operator client references must be distinct"
        )

    session_mode = payload["session_mode"]
    if session_mode != "access_token_sha256":
        raise OidcStagingEvidenceError(
            "session_mode must be access_token_sha256"
        )
    configured_lifetime = payload[
        "configured_max_access_token_lifetime_seconds"
    ]
    if (
        type(configured_lifetime) is not int
        or configured_lifetime != _MAX_ACCESS_TOKEN_LIFETIME_SECONDS
    ):
        raise OidcStagingEvidenceError(
            "configured maximum access token lifetime must be exactly 900 seconds"
        )

    status = payload["status"]
    if status not in {"passed", "failed"}:
        raise OidcStagingEvidenceError("status must be passed or failed")
    counts = payload["counts_as_staging_oidc_evidence"]
    if type(counts) is not bool:
        raise OidcStagingEvidenceError(
            "counts_as_staging_oidc_evidence must be boolean"
        )
    privacy_fields = (
        "contains_access_token",
        "contains_authorization_header",
        "contains_id_token",
        "contains_invite_token",
        "contains_oauth_code_or_verifier",
        "contains_personal_identity",
        "contains_refresh_token",
    )
    if any(type(payload[name]) is not bool for name in privacy_fields):
        raise OidcStagingEvidenceError("privacy flags must be boolean")
    if any(payload[name] for name in privacy_fields):
        raise OidcStagingEvidenceError(
            "OIDC evidence must not contain sensitive authentication material"
        )

    checks = _checks(payload["checks"])
    artifact_hashes = _artifact_hashes(payload["artifact_sha256"])
    failure_codes = _failure_codes(payload["failure_codes"])
    product_lifetime = _lifetime(
        payload["observed_product_access_token_lifetime_seconds"],
        field_name="product access token lifetime",
        allow_zero=status == "failed",
    )
    operator_lifetime = _lifetime(
        payload["observed_operator_access_token_lifetime_seconds"],
        field_name="operator access token lifetime",
        allow_zero=status == "failed",
    )

    if status == "passed":
        if counts is not True:
            raise OidcStagingEvidenceError(
                "passed evidence must count as staging OIDC evidence"
            )
        if any(value != "passed" for value in checks.values()):
            raise OidcStagingEvidenceError(
                "passed evidence requires every check to pass"
            )
        if failure_codes:
            raise OidcStagingEvidenceError(
                "passed evidence must not contain failure_codes"
            )
    else:
        if counts is not False:
            raise OidcStagingEvidenceError(
                "failed evidence cannot count as staging OIDC evidence"
            )
        if all(value == "passed" for value in checks.values()):
            raise OidcStagingEvidenceError(
                "failed evidence requires at least one failed check"
            )
        if not failure_codes:
            raise OidcStagingEvidenceError(
                "failed evidence requires failure_codes"
            )

    return OidcStagingEvidence(
        run_id=run_id,
        release_revision=release,
        edition_date=edition_date,
        started_at=started_at,
        finished_at=finished_at,
        identity_provider=provider,
        tenant_reference_sha256=tenant_reference,
        product_client_reference_sha256=product_client_reference,
        operator_client_reference_sha256=operator_client_reference,
        api_audience_reference_sha256=audience_reference,
        session_mode=session_mode,
        configured_max_access_token_lifetime_seconds=configured_lifetime,
        observed_product_access_token_lifetime_seconds=product_lifetime,
        observed_operator_access_token_lifetime_seconds=operator_lifetime,
        status=status,
        counts_as_staging_oidc_evidence=counts,
        checks=checks,
        failure_codes=failure_codes,
        artifact_sha256=artifact_hashes,
        evidence_sha256=_canonical_sha256(payload),
    )


__all__ = [
    "OidcStagingEvidence",
    "OidcStagingEvidenceError",
    "REQUIRED_OIDC_STAGING_CHECKS",
    "parse_oidc_staging_evidence",
]
