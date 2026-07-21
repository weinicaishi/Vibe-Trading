"""Build one strict staging-day manifest from eleven gate artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
import re
from typing import Any

from src.market_morning.db import EXPECTED_MARKET_MORNING_SCHEMA_REVISION
from src.market_morning.release_candidate import ReleaseCandidateManifest
from src.market_morning.staging_evidence import (
    REQUIRED_STAGING_GATES,
    StagingEvidenceError,
    parse_staging_day_evidence,
)


_ARTIFACT_FIELDS = frozenset(
    {
        "schema_version",
        "scope",
        "environment_tier",
        "gate",
        "run_id",
        "release_candidate_sha256",
        "release_revision",
        "runtime_schema_revision",
        "edition_date",
        "previous_jpx_open_date",
        "next_jpx_open_date",
        "calendar_provider",
        "provider_ids",
        "started_at",
        "finished_at",
        "published_at",
        "status",
        "contains_fixture_data",
        "contains_synthetic_data",
        "model_invocation_count",
        "failure_code",
        "evidence_sha256",
    }
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_FAILURE_CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,48}$")
_JST_OFFSET = timedelta(hours=9)


@dataclass(frozen=True, slots=True)
class StagingGateArtifact:
    gate: str
    run_id: str
    release_candidate_sha256: str
    release_revision: str
    runtime_schema_revision: str
    edition_date: date
    previous_jpx_open_date: date
    next_jpx_open_date: date
    calendar_provider: str
    provider_ids: tuple[str, ...]
    started_at: datetime
    finished_at: datetime
    published_at: datetime | None
    status: str
    model_invocation_count: int
    failure_code: str | None
    evidence_sha256: str
    artifact_sha256: str


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_string(value: Any, *, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise StagingEvidenceError(f"{field_name} must be a string")
    canonical = value.strip()
    if not canonical or canonical != value or len(canonical) > maximum:
        raise StagingEvidenceError(f"{field_name} is invalid")
    return canonical


def _safe_provider(value: Any, *, field_name: str) -> str:
    provider = _safe_string(value, field_name=field_name, maximum=128)
    if any(marker in provider.lower() for marker in ("fixture", "synthetic", "demo")):
        raise StagingEvidenceError(f"{field_name} must identify production data")
    return provider


def _iso_date(value: Any, *, field_name: str) -> date:
    if not isinstance(value, str):
        raise StagingEvidenceError(f"{field_name} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise StagingEvidenceError(f"{field_name} must be an ISO date") from None
    if parsed.isoformat() != value:
        raise StagingEvidenceError(f"{field_name} must be an ISO date")
    return parsed


def _jst_datetime(value: Any, *, field_name: str, edition_date: date) -> datetime:
    if not isinstance(value, str):
        raise StagingEvidenceError(f"{field_name} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise StagingEvidenceError(f"{field_name} must be an ISO datetime") from None
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != _JST_OFFSET
        or parsed.date() != edition_date
    ):
        raise StagingEvidenceError(f"{field_name} must use JST on edition_date")
    return parsed


def parse_staging_gate_artifact(payload: Mapping[str, Any]) -> StagingGateArtifact:
    """Parse one deployment-produced gate artifact without accepting extras."""

    if not isinstance(payload, Mapping) or frozenset(payload) != _ARTIFACT_FIELDS:
        raise StagingEvidenceError("staging gate artifact fields are incomplete or unexpected")
    if payload["schema_version"] != 1:
        raise StagingEvidenceError("staging gate artifact schema is unsupported")
    if payload["scope"] != "market_morning_staging_gate_artifact":
        raise StagingEvidenceError("staging gate artifact scope is invalid")
    if payload["environment_tier"] != "staging":
        raise StagingEvidenceError("staging gate artifact environment must be staging")
    gate = payload["gate"]
    if gate not in REQUIRED_STAGING_GATES:
        raise StagingEvidenceError("staging gate artifact gate is invalid")
    run_id = _safe_string(payload["run_id"], field_name="run_id", maximum=128)
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        raise StagingEvidenceError("run_id contains unsupported characters")
    candidate_hash = payload["release_candidate_sha256"]
    if not isinstance(candidate_hash, str) or not _SHA256_PATTERN.fullmatch(
        candidate_hash
    ):
        raise StagingEvidenceError("release_candidate_sha256 is invalid")
    release_revision = payload["release_revision"]
    if not isinstance(release_revision, str) or not _RELEASE_PATTERN.fullmatch(
        release_revision
    ):
        raise StagingEvidenceError("release_revision is invalid")
    runtime_schema = payload["runtime_schema_revision"]
    if runtime_schema != EXPECTED_MARKET_MORNING_SCHEMA_REVISION:
        raise StagingEvidenceError("runtime_schema_revision is not current")

    edition_date = _iso_date(payload["edition_date"], field_name="edition_date")
    previous_open = _iso_date(
        payload["previous_jpx_open_date"],
        field_name="previous_jpx_open_date",
    )
    next_open = _iso_date(
        payload["next_jpx_open_date"],
        field_name="next_jpx_open_date",
    )
    if not previous_open < edition_date < next_open:
        raise StagingEvidenceError("JPX open-session links must surround edition_date")
    calendar_provider = _safe_provider(
        payload["calendar_provider"],
        field_name="calendar_provider",
    )
    provider_values = payload["provider_ids"]
    if not isinstance(provider_values, list):
        raise StagingEvidenceError("provider_ids must be a list")
    providers = tuple(
        _safe_provider(value, field_name="provider_ids")
        for value in provider_values
    )
    if len(providers) != len(set(providers)):
        raise StagingEvidenceError("provider_ids must be unique")

    started_at = _jst_datetime(
        payload["started_at"],
        field_name="started_at",
        edition_date=edition_date,
    )
    finished_at = _jst_datetime(
        payload["finished_at"],
        field_name="finished_at",
        edition_date=edition_date,
    )
    if started_at > finished_at:
        raise StagingEvidenceError("finished_at must not precede started_at")
    published_value = payload["published_at"]
    published_at = (
        None
        if published_value is None
        else _jst_datetime(
            published_value,
            field_name="published_at",
            edition_date=edition_date,
        )
    )
    if gate == "publication":
        if payload["status"] == "passed" and published_at is None:
            raise StagingEvidenceError("passed publication artifact requires published_at")
    elif published_at is not None:
        raise StagingEvidenceError("only publication artifact may set published_at")

    status = payload["status"]
    if status not in {"passed", "failed"}:
        raise StagingEvidenceError("staging gate artifact status is invalid")
    if payload["contains_fixture_data"] is not False:
        raise StagingEvidenceError("fixture data cannot enter staging evidence")
    if payload["contains_synthetic_data"] is not False:
        raise StagingEvidenceError("synthetic data cannot enter staging evidence")
    model_invocations = payload["model_invocation_count"]
    if type(model_invocations) is not int or model_invocations < 0:
        raise StagingEvidenceError("model_invocation_count must be a non-negative integer")
    if gate == "model_usage_cost":
        if status == "passed" and model_invocations < 1:
            raise StagingEvidenceError("passed model_usage_cost requires an invocation")
    elif model_invocations != 0:
        raise StagingEvidenceError("only model_usage_cost may report model invocations")
    failure_code = payload["failure_code"]
    if status == "passed":
        if failure_code is not None:
            raise StagingEvidenceError("passed gate artifact cannot have failure_code")
    elif not isinstance(failure_code, str) or not _FAILURE_CODE_PATTERN.fullmatch(
        failure_code
    ):
        raise StagingEvidenceError("failed gate artifact requires a stable failure_code")
    if failure_code is not None and len(f"{gate}_{failure_code}") > 64:
        raise StagingEvidenceError("combined gate failure code is too long")
    evidence_sha256 = payload["evidence_sha256"]
    if not isinstance(evidence_sha256, str) or not _SHA256_PATTERN.fullmatch(
        evidence_sha256
    ):
        raise StagingEvidenceError("evidence_sha256 is invalid")

    return StagingGateArtifact(
        gate=gate,
        run_id=run_id,
        release_candidate_sha256=candidate_hash,
        release_revision=release_revision,
        runtime_schema_revision=runtime_schema,
        edition_date=edition_date,
        previous_jpx_open_date=previous_open,
        next_jpx_open_date=next_open,
        calendar_provider=calendar_provider,
        provider_ids=providers,
        started_at=started_at,
        finished_at=finished_at,
        published_at=published_at,
        status=status,
        model_invocation_count=model_invocations,
        failure_code=failure_code,
        evidence_sha256=evidence_sha256,
        artifact_sha256=_canonical_sha256(payload),
    )


def build_staging_gate_artifact_payload(
    *,
    release_candidate: ReleaseCandidateManifest,
    gate: str,
    run_id: str,
    edition_date: str,
    previous_jpx_open_date: str,
    next_jpx_open_date: str,
    calendar_provider: str,
    provider_ids: tuple[str, ...],
    started_at: str,
    finished_at: str,
    published_at: str | None,
    status: str,
    model_invocation_count: int,
    failure_code: str | None,
    evidence_sha256: str,
) -> dict[str, Any]:
    """Create one gate envelope while deriving immutable release identity."""

    if release_candidate.status != "passed" or release_candidate.release_revision is None:
        raise StagingEvidenceError("release candidate must be passed")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "scope": "market_morning_staging_gate_artifact",
        "environment_tier": "staging",
        "gate": gate,
        "run_id": run_id,
        "release_candidate_sha256": release_candidate.manifest_sha256,
        "release_revision": release_candidate.release_revision,
        "runtime_schema_revision": EXPECTED_MARKET_MORNING_SCHEMA_REVISION,
        "edition_date": edition_date,
        "previous_jpx_open_date": previous_jpx_open_date,
        "next_jpx_open_date": next_jpx_open_date,
        "calendar_provider": calendar_provider,
        "provider_ids": list(provider_ids),
        "started_at": started_at,
        "finished_at": finished_at,
        "published_at": published_at,
        "status": status,
        "contains_fixture_data": False,
        "contains_synthetic_data": False,
        "model_invocation_count": model_invocation_count,
        "failure_code": failure_code,
        "evidence_sha256": evidence_sha256,
    }
    parse_staging_gate_artifact(payload)
    return payload


def build_staging_day_evidence(
    *,
    release_candidate: ReleaseCandidateManifest,
    artifacts: Mapping[str, StagingGateArtifact],
) -> dict[str, Any]:
    """Bind all gate artifacts to a passed release and emit the strict day schema."""

    if release_candidate.status != "passed" or release_candidate.release_revision is None:
        raise StagingEvidenceError("release candidate must be passed")
    if frozenset(artifacts) != REQUIRED_STAGING_GATES:
        raise StagingEvidenceError("every required staging gate artifact is required")
    if any(name != artifact.gate for name, artifact in artifacts.items()):
        raise StagingEvidenceError("staging gate artifact name does not match payload")

    ordered = tuple(artifacts[name] for name in sorted(REQUIRED_STAGING_GATES))
    first = ordered[0]
    shared_fields = (
        "run_id",
        "release_candidate_sha256",
        "release_revision",
        "runtime_schema_revision",
        "edition_date",
        "previous_jpx_open_date",
        "next_jpx_open_date",
        "calendar_provider",
    )
    for artifact in ordered[1:]:
        if any(getattr(artifact, field) != getattr(first, field) for field in shared_fields):
            raise StagingEvidenceError("staging gate artifacts do not share one run identity")
    if first.release_candidate_sha256 != release_candidate.manifest_sha256:
        raise StagingEvidenceError("staging artifacts do not reference the supplied release candidate")
    if first.release_revision != release_candidate.release_revision:
        raise StagingEvidenceError("staging artifacts use a different release revision")

    provider_ids = sorted(
        {provider for artifact in ordered for provider in artifact.provider_ids}
    )
    if len(provider_ids) < 5:
        raise StagingEvidenceError("staging artifacts require at least five production providers")
    publication = artifacts["publication"]
    model_usage = artifacts["model_usage_cost"]
    all_passed = all(artifact.status == "passed" for artifact in ordered)
    failure_codes = [
        f"{artifact.gate}_{artifact.failure_code}"
        for artifact in ordered
        if artifact.failure_code is not None
    ]
    payload: dict[str, Any] = {
        "schema_version": 1,
        "scope": "market_morning_staging_day",
        "environment_tier": "staging",
        "run_id": first.run_id,
        "release_revision": first.release_revision,
        "runtime_schema_revision": first.runtime_schema_revision,
        "edition_date": first.edition_date.isoformat(),
        "previous_jpx_open_date": first.previous_jpx_open_date.isoformat(),
        "next_jpx_open_date": first.next_jpx_open_date.isoformat(),
        "calendar_provider": first.calendar_provider,
        "provider_ids": provider_ids,
        "started_at": min(item.started_at for item in ordered).isoformat(),
        "finished_at": max(item.finished_at for item in ordered).isoformat(),
        "published_at": (
            publication.published_at.isoformat()
            if publication.published_at is not None
            else None
        ),
        "status": "passed" if all_passed else "failed",
        "counts_as_staging_day": all_passed,
        "contains_fixture_data": False,
        "contains_synthetic_data": False,
        "model_invocation_count": model_usage.model_invocation_count,
        "gates": {name: artifacts[name].status for name in sorted(artifacts)},
        "failure_codes": failure_codes,
        "artifact_sha256": {
            name: artifacts[name].artifact_sha256 for name in sorted(artifacts)
        },
    }
    parse_staging_day_evidence(payload)
    return payload


__all__ = [
    "StagingGateArtifact",
    "build_staging_gate_artifact_payload",
    "build_staging_day_evidence",
    "parse_staging_gate_artifact",
]
