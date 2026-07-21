"""Fail-closed evidence contracts for the five-day Market Morning T1 gate."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import hashlib
import json
import re
from typing import Any

from src.market_morning.db import EXPECTED_MARKET_MORNING_SCHEMA_REVISION


REQUIRED_CONSECUTIVE_STAGING_DAYS = 5
REQUIRED_STAGING_GATES = frozenset(
    {
        "database_readiness",
        "deployment_preflight",
        "runtime_preflight",
        "oidc_session",
        "licensed_sources",
        "market_snapshots",
        "content_quality",
        "publication",
        "email_delivery",
        "model_usage_cost",
        "observability",
    }
)

_EXPECTED_FIELDS = frozenset(
    {
        "schema_version",
        "scope",
        "environment_tier",
        "run_id",
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
        "counts_as_staging_day",
        "contains_fixture_data",
        "contains_synthetic_data",
        "model_invocation_count",
        "gates",
        "failure_codes",
        "artifact_sha256",
    }
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_FAILURE_CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
_JST_OFFSET = timedelta(hours=9)


class StagingEvidenceError(ValueError):
    """Raised when evidence cannot safely count toward the T1 gate."""


def _required_string(value: Any, *, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise StagingEvidenceError(f"{field_name} must be a string")
    canonical = value.strip()
    if not canonical or len(canonical) > maximum:
        raise StagingEvidenceError(f"{field_name} must contain 1 to {maximum} characters")
    return canonical


def _parse_date(value: Any, *, field_name: str) -> date:
    if not isinstance(value, str):
        raise StagingEvidenceError(f"{field_name} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise StagingEvidenceError(f"{field_name} must be an ISO date") from error
    if parsed.isoformat() != value:
        raise StagingEvidenceError(f"{field_name} must be an ISO date")
    return parsed


def _parse_jst_datetime(
    value: Any,
    *,
    field_name: str,
    edition_date: date,
) -> datetime:
    if not isinstance(value, str):
        raise StagingEvidenceError(f"{field_name} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise StagingEvidenceError(f"{field_name} must be an ISO datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() != _JST_OFFSET:
        raise StagingEvidenceError(f"{field_name} must use explicit JST +09:00")
    if parsed.date() != edition_date:
        raise StagingEvidenceError(f"{field_name} must match edition_date")
    return parsed


def _safe_provider(value: Any, *, field_name: str) -> str:
    provider = _required_string(value, field_name=field_name, maximum=128)
    lowered = provider.lower()
    if any(marker in lowered for marker in ("fixture", "synthetic", "demo")):
        raise StagingEvidenceError(f"{field_name} must not identify fixture or synthetic data")
    return provider


def _parse_gate_map(value: Any, *, field_name: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise StagingEvidenceError(f"{field_name} must be an object")
    keys = frozenset(value)
    if keys != REQUIRED_STAGING_GATES:
        raise StagingEvidenceError(f"{field_name} must contain every required staging gate exactly once")
    result: dict[str, str] = {}
    for name in sorted(REQUIRED_STAGING_GATES):
        status = value[name]
        if status not in {"passed", "failed"}:
            raise StagingEvidenceError(f"{field_name} values must be passed or failed")
        result[name] = status
    return result


def _parse_artifact_hashes(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise StagingEvidenceError("artifact_sha256 must be an object")
    if frozenset(value) != REQUIRED_STAGING_GATES:
        raise StagingEvidenceError("artifact_sha256 must contain every required staging gate")
    result: dict[str, str] = {}
    for name in sorted(REQUIRED_STAGING_GATES):
        digest = value[name]
        if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
            raise StagingEvidenceError("artifact_sha256 values must be lowercase SHA-256 digests")
        result[name] = digest
    return result


def _parse_failure_codes(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise StagingEvidenceError("failure_codes must be a list")
    if len(value) != len(set(value)):
        raise StagingEvidenceError("failure_codes must be unique")
    codes: list[str] = []
    for code in value:
        if not isinstance(code, str) or not _FAILURE_CODE_PATTERN.fullmatch(code):
            raise StagingEvidenceError("failure_codes must contain stable lowercase codes")
        codes.append(code)
    return tuple(codes)


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class StagingDayEvidence:
    run_id: str
    release_revision: str
    edition_date: date
    previous_jpx_open_date: date
    next_jpx_open_date: date
    calendar_provider: str
    started_at: datetime
    finished_at: datetime
    published_at: datetime | None
    status: str
    counts_as_staging_day: bool
    model_invocation_count: int
    gates: Mapping[str, str]
    failure_codes: tuple[str, ...]
    evidence_sha256: str

    def to_summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "release_revision": self.release_revision,
            "edition_date": self.edition_date.isoformat(),
            "status": self.status,
            "counts_as_staging_day": self.counts_as_staging_day,
            "failure_codes": list(self.failure_codes),
            "evidence_sha256": self.evidence_sha256,
        }


def parse_staging_day_evidence(payload: Mapping[str, Any]) -> StagingDayEvidence:
    """Parse one strict staging-day manifest without accepting extra fields."""

    if not isinstance(payload, Mapping):
        raise StagingEvidenceError("staging evidence must be an object")
    unexpected = frozenset(payload) - _EXPECTED_FIELDS
    missing = _EXPECTED_FIELDS - frozenset(payload)
    if unexpected:
        raise StagingEvidenceError("staging evidence contains unexpected fields")
    if missing:
        raise StagingEvidenceError("staging evidence is missing required fields")
    if payload["schema_version"] != 1:
        raise StagingEvidenceError("schema_version is unsupported")
    if payload["scope"] != "market_morning_staging_day":
        raise StagingEvidenceError("scope is not a staging-day manifest")
    if payload["environment_tier"] != "staging":
        raise StagingEvidenceError("environment_tier must be staging")
    run_id = _required_string(payload["run_id"], field_name="run_id", maximum=128)
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        raise StagingEvidenceError("run_id contains unsupported characters")
    release_revision = payload["release_revision"]
    if not isinstance(release_revision, str) or not _RELEASE_PATTERN.fullmatch(release_revision):
        raise StagingEvidenceError("release_revision must be a lowercase immutable revision")
    if payload["runtime_schema_revision"] != EXPECTED_MARKET_MORNING_SCHEMA_REVISION:
        raise StagingEvidenceError("runtime_schema_revision is not current")

    edition_date = _parse_date(payload["edition_date"], field_name="edition_date")
    previous_open = _parse_date(
        payload["previous_jpx_open_date"],
        field_name="previous_jpx_open_date",
    )
    next_open = _parse_date(
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
    if not isinstance(provider_values, list) or not provider_values:
        raise StagingEvidenceError("provider_ids must be a non-empty list")
    providers = tuple(_safe_provider(value, field_name="provider_ids") for value in provider_values)
    if len(providers) < 5:
        raise StagingEvidenceError("provider_ids must contain at least five production providers")
    if len(providers) != len(set(providers)):
        raise StagingEvidenceError("provider_ids must be unique")

    started_at = _parse_jst_datetime(
        payload["started_at"],
        field_name="started_at",
        edition_date=edition_date,
    )
    finished_at = _parse_jst_datetime(
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
        else _parse_jst_datetime(
            published_value,
            field_name="published_at",
            edition_date=edition_date,
        )
    )
    if published_at is not None:
        if published_at.timetz().replace(tzinfo=None) < time(6, 30):
            raise StagingEvidenceError("published_at must be no earlier than 06:30 JST")
        if published_at.timetz().replace(tzinfo=None) > time(8, 30):
            raise StagingEvidenceError("published_at must be no later than 08:30 JST")
        if not started_at <= published_at <= finished_at:
            raise StagingEvidenceError("published_at must fall within the staging run")

    status = payload["status"]
    if status not in {"passed", "failed"}:
        raise StagingEvidenceError("status must be passed or failed")
    counts = payload["counts_as_staging_day"]
    if type(counts) is not bool:
        raise StagingEvidenceError("counts_as_staging_day must be boolean")
    contains_fixture = payload["contains_fixture_data"]
    contains_synthetic = payload["contains_synthetic_data"]
    if type(contains_fixture) is not bool or type(contains_synthetic) is not bool:
        raise StagingEvidenceError("fixture and synthetic flags must be boolean")
    if contains_fixture:
        raise StagingEvidenceError("fixture data cannot count as staging evidence")
    if contains_synthetic:
        raise StagingEvidenceError("synthetic data cannot count as staging evidence")
    model_invocations = payload["model_invocation_count"]
    if type(model_invocations) is not int or model_invocations < 0:
        raise StagingEvidenceError("model_invocation_count must be a non-negative integer")
    gates = _parse_gate_map(payload["gates"], field_name="gates")
    failure_codes = _parse_failure_codes(payload["failure_codes"])
    _parse_artifact_hashes(payload["artifact_sha256"])

    if status == "passed":
        if counts is not True:
            raise StagingEvidenceError("passed evidence requires counts_as_staging_day=true")
        if published_at is None:
            raise StagingEvidenceError("passed evidence requires published_at")
        if any(value != "passed" for value in gates.values()):
            raise StagingEvidenceError("passed evidence requires every gate to pass")
        if failure_codes:
            raise StagingEvidenceError("passed evidence must not contain failure_codes")
        if model_invocations < 1:
            raise StagingEvidenceError("passed evidence requires model_invocation_count above zero")
    else:
        if counts is not False:
            raise StagingEvidenceError("failed evidence requires counts_as_staging_day=false")
        if all(value == "passed" for value in gates.values()):
            raise StagingEvidenceError("failed evidence requires a failed gate")
        if not failure_codes:
            raise StagingEvidenceError("failed evidence requires failure_codes")

    return StagingDayEvidence(
        run_id=run_id,
        release_revision=release_revision,
        edition_date=edition_date,
        previous_jpx_open_date=previous_open,
        next_jpx_open_date=next_open,
        calendar_provider=calendar_provider,
        started_at=started_at,
        finished_at=finished_at,
        published_at=published_at,
        status=status,
        counts_as_staging_day=counts,
        model_invocation_count=model_invocations,
        gates=gates,
        failure_codes=failure_codes,
        evidence_sha256=_canonical_sha256(payload),
    )


@dataclass(frozen=True, slots=True)
class T1StagingGateReport:
    status: str
    required_consecutive_days: int
    trailing_consecutive_days: int
    longest_consecutive_days: int
    eligible_dates: tuple[date, ...]
    evaluated_days: tuple[StagingDayEvidence, ...]
    blocking_codes: tuple[str, ...]

    @property
    def counts_as_t1_evidence(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "scope": "market_morning_t1_staging_gate",
            "status": self.status,
            "counts_as_t1_evidence": self.counts_as_t1_evidence,
            "required_consecutive_days": self.required_consecutive_days,
            "trailing_consecutive_days": self.trailing_consecutive_days,
            "longest_consecutive_days": self.longest_consecutive_days,
            "eligible_dates": [value.isoformat() for value in self.eligible_dates],
            "blocking_codes": list(self.blocking_codes),
            "days": [day.to_summary() for day in self.evaluated_days],
        }


def evaluate_t1_staging_gate(
    evidence: tuple[StagingDayEvidence, ...],
) -> T1StagingGateReport:
    """Require the latest five linked JPX sessions on one release to pass."""

    ordered = tuple(sorted(evidence, key=lambda item: item.edition_date))
    dates = [item.edition_date for item in ordered]
    if len(dates) != len(set(dates)):
        raise StagingEvidenceError("duplicate edition_date is not allowed")

    current: list[StagingDayEvidence] = []
    longest = 0
    diagnostics: list[str] = []
    previous: StagingDayEvidence | None = None
    for day in ordered:
        linked = previous is None
        compatible = previous is None
        if previous is not None:
            forward = previous.next_jpx_open_date == day.edition_date
            backward = day.previous_jpx_open_date == previous.edition_date
            if forward != backward:
                raise StagingEvidenceError("JPX session links are inconsistent")
            linked = forward and backward
            if not linked:
                diagnostics.append("jpx_session_gap")
            compatible = (
                previous.release_revision == day.release_revision
                and previous.calendar_provider == day.calendar_provider
            )
            if previous.release_revision != day.release_revision:
                diagnostics.append("release_changed")
            if previous.calendar_provider != day.calendar_provider:
                diagnostics.append("calendar_provider_changed")

        if not day.counts_as_staging_day:
            diagnostics.append("staging_day_failed")
            current = []
        elif previous is None or (linked and compatible and current):
            current.append(day)
        else:
            current = [day]
        longest = max(longest, len(current))
        previous = day

    passed = len(current) >= REQUIRED_CONSECUTIVE_STAGING_DAYS
    blocking_codes: tuple[str, ...]
    if passed:
        blocking_codes = ()
    else:
        if not ordered:
            diagnostics.append("no_staging_evidence")
        diagnostics.append("insufficient_consecutive_days")
        blocking_codes = tuple(dict.fromkeys(diagnostics))
    return T1StagingGateReport(
        status="passed" if passed else "not_ready",
        required_consecutive_days=REQUIRED_CONSECUTIVE_STAGING_DAYS,
        trailing_consecutive_days=len(current),
        longest_consecutive_days=longest,
        eligible_dates=tuple(item.edition_date for item in current),
        evaluated_days=ordered,
        blocking_codes=blocking_codes,
    )


__all__ = [
    "REQUIRED_CONSECUTIVE_STAGING_DAYS",
    "REQUIRED_STAGING_GATES",
    "StagingDayEvidence",
    "StagingEvidenceError",
    "T1StagingGateReport",
    "evaluate_t1_staging_gate",
    "parse_staging_day_evidence",
]
