"""Fail-closed aggregate evidence gate for enabling Market Morning T1."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import re
from typing import Any

from src.market_morning.db import EXPECTED_MARKET_MORNING_SCHEMA_REVISION
from src.market_morning.mysql_acceptance import MYSQL_ACCEPTANCE_SCENARIOS
from src.market_morning.mysql_migration_rehearsal import MYSQL_MIGRATION_SCENARIOS


REQUIRED_EXTERNAL_SIGNOFFS = frozenset(
    {
        "tdnet_rights",
        "edinet_rights",
        "company_ir_rights",
        "jpx_market_data_rights",
        "japan_legal_review",
    }
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_EXTERNAL_FIELDS = frozenset(
    {
        "schema_version",
        "scope",
        "environment_tier",
        "release_revision",
        "runtime_schema_revision",
        "generated_at",
        "signoffs",
    }
)
_SIGNOFF_FIELDS = frozenset(
    {
        "status",
        "approval_reference",
        "approved_at",
        "review_due_on",
        "evidence_sha256",
    }
)
_STAGING_GATE_FIELDS = frozenset(
    {
        "schema_version",
        "scope",
        "status",
        "counts_as_t1_evidence",
        "required_consecutive_days",
        "trailing_consecutive_days",
        "longest_consecutive_days",
        "eligible_dates",
        "blocking_codes",
        "days",
    }
)


class T1ReleaseEvidenceError(ValueError):
    """Raised when an input cannot safely participate in a release decision."""


def _mapping(value: Any, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise T1ReleaseEvidenceError(f"{field_name} must be an object")
    return value


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise T1ReleaseEvidenceError("evidence must be JSON serializable") from error
    return hashlib.sha256(encoded).hexdigest()


def _sha256(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise T1ReleaseEvidenceError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _nonnegative_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise T1ReleaseEvidenceError(f"{field_name} must be a non-negative integer")
    return value


def _parse_mysql_manifest(
    payload: Mapping[str, Any],
    *,
    label: str,
    scope: str,
    scenario_ids: frozenset[str],
    destructive: bool,
) -> tuple[bool, str]:
    required = {
        "schema_version",
        "scope",
        "target_fingerprint",
        "status",
        "counts_as_staging_day",
        "contains_destructive_migration_evidence",
        "summary",
        "scenarios",
    }
    if not required <= set(payload):
        raise T1ReleaseEvidenceError(f"{label} evidence is missing required fields")
    if payload["schema_version"] != 1 or payload["scope"] != scope:
        raise T1ReleaseEvidenceError(f"{label} evidence has the wrong contract")
    if payload.get("environment") != "staging":
        raise T1ReleaseEvidenceError(
            f"{label} evidence must come from the staging environment"
        )
    if payload["counts_as_staging_day"] is not False:
        raise T1ReleaseEvidenceError(f"{label} evidence cannot count as a staging day")
    if payload["contains_destructive_migration_evidence"] is not destructive:
        raise T1ReleaseEvidenceError(f"{label} destructive evidence marker is invalid")
    fingerprint = _sha256(
        payload["target_fingerprint"],
        field_name=f"{label}.target_fingerprint",
    )
    status = payload["status"]
    if status not in {"passed", "failed"}:
        raise T1ReleaseEvidenceError(f"{label} status must be passed or failed")

    summary = _mapping(payload["summary"], field_name=f"{label}.summary")
    if frozenset(summary) != {"passed", "failed", "errors", "total"}:
        raise T1ReleaseEvidenceError(f"{label} summary fields are invalid")
    counts = {
        key: _nonnegative_int(summary[key], field_name=f"{label}.summary.{key}")
        for key in ("passed", "failed", "errors", "total")
    }
    if counts["total"] != len(scenario_ids) or (
        counts["passed"] + counts["failed"] + counts["errors"] != counts["total"]
    ):
        raise T1ReleaseEvidenceError(f"{label} summary counts are inconsistent")

    raw_scenarios = payload["scenarios"]
    if not isinstance(raw_scenarios, list) or len(raw_scenarios) != len(scenario_ids):
        raise T1ReleaseEvidenceError(f"{label} must contain every required scenario")
    observed: set[str] = set()
    observed_counts = {"passed": 0, "failed": 0, "errors": 0}
    for raw in raw_scenarios:
        scenario = _mapping(raw, field_name=f"{label}.scenario")
        scenario_id = scenario.get("scenario_id")
        scenario_status = scenario.get("status")
        if not isinstance(scenario_id, str) or scenario_id in observed:
            raise T1ReleaseEvidenceError(f"{label} scenario IDs are invalid")
        if scenario_status not in observed_counts:
            raise T1ReleaseEvidenceError(f"{label} scenario status is invalid")
        observed.add(scenario_id)
        observed_counts[scenario_status] += 1
        if scenario_status == "passed":
            if scenario.get("exit_code") != 0:
                raise T1ReleaseEvidenceError(f"{label} passed scenario must have exit code zero")
            _sha256(
                scenario.get("output_sha256"),
                field_name=f"{label}.scenario.output_sha256",
            )
    if observed != set(scenario_ids) or observed_counts != {
        "passed": counts["passed"],
        "failed": counts["failed"],
        "errors": counts["errors"],
    }:
        raise T1ReleaseEvidenceError(f"{label} scenario results are inconsistent")
    passed = status == "passed"
    if passed != (counts["passed"] == counts["total"]):
        raise T1ReleaseEvidenceError(f"{label} aggregate status is inconsistent")
    return passed, fingerprint


@dataclass(frozen=True, slots=True)
class _StagingGate:
    passed: bool
    release_revision: str | None


def _parse_staging_gate(payload: Mapping[str, Any]) -> _StagingGate:
    if frozenset(payload) != _STAGING_GATE_FIELDS:
        raise T1ReleaseEvidenceError("staging gate fields are invalid")
    if payload["schema_version"] != 1 or payload["scope"] != "market_morning_t1_staging_gate":
        raise T1ReleaseEvidenceError("staging gate has the wrong contract")
    status = payload["status"]
    if status not in {"passed", "not_ready"}:
        raise T1ReleaseEvidenceError("staging gate status is invalid")
    counts = payload["counts_as_t1_evidence"]
    if not isinstance(counts, bool) or counts != (status == "passed"):
        raise T1ReleaseEvidenceError("staging gate evidence marker is inconsistent")
    required_days = _nonnegative_int(
        payload["required_consecutive_days"], field_name="required_consecutive_days"
    )
    trailing_days = _nonnegative_int(
        payload["trailing_consecutive_days"], field_name="trailing_consecutive_days"
    )
    _nonnegative_int(payload["longest_consecutive_days"], field_name="longest_consecutive_days")
    if required_days != 5:
        raise T1ReleaseEvidenceError("staging gate must require five consecutive days")
    blocking = payload["blocking_codes"]
    if not isinstance(blocking, list) or any(not isinstance(code, str) or not code for code in blocking):
        raise T1ReleaseEvidenceError("staging blocking codes are invalid")
    eligible_dates = payload["eligible_dates"]
    days = payload["days"]
    if not isinstance(eligible_dates, list) or not isinstance(days, list):
        raise T1ReleaseEvidenceError("staging gate day summaries are invalid")
    if status == "passed" and (
        trailing_days < required_days
        or len(eligible_dates) < required_days
        or blocking
        or len(days) < trailing_days
    ):
        raise T1ReleaseEvidenceError("passed staging gate is internally inconsistent")
    if status == "not_ready" and not blocking:
        raise T1ReleaseEvidenceError("not-ready staging gate requires blocking codes")

    release_revision: str | None = None
    if days:
        trailing = days[-trailing_days:] if trailing_days else []
        releases: set[str] = set()
        for raw in trailing:
            day = _mapping(raw, field_name="staging day")
            release = day.get("release_revision")
            if not isinstance(release, str) or not _RELEASE_PATTERN.fullmatch(release):
                raise T1ReleaseEvidenceError("staging release revision is invalid")
            if day.get("status") != "passed" or day.get("counts_as_staging_day") is not True:
                raise T1ReleaseEvidenceError("eligible staging day is not passed")
            _sha256(day.get("evidence_sha256"), field_name="staging day evidence_sha256")
            releases.add(release)
        if len(releases) > 1:
            raise T1ReleaseEvidenceError("trailing staging days use different releases")
        if releases:
            release_revision = next(iter(releases))
    if status == "passed" and release_revision is None:
        raise T1ReleaseEvidenceError("passed staging gate must identify a release")
    return _StagingGate(passed=status == "passed", release_revision=release_revision)


@dataclass(frozen=True, slots=True)
class _ExternalSignoffs:
    release_revision: str
    runtime_schema_revision: str
    blocking_codes: tuple[str, ...]


def _parse_aware_datetime(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise T1ReleaseEvidenceError(f"{field_name} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise T1ReleaseEvidenceError(f"{field_name} must be an ISO datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != value:
        raise T1ReleaseEvidenceError(f"{field_name} must include an explicit offset")
    return parsed


def _parse_external_signoffs(payload: Mapping[str, Any]) -> _ExternalSignoffs:
    if frozenset(payload) != _EXTERNAL_FIELDS:
        raise T1ReleaseEvidenceError("external signoff fields are invalid")
    if (
        payload["schema_version"] != 2
        or payload["scope"] != "market_morning_t1_external_signoffs"
        or payload["environment_tier"] != "staging"
    ):
        raise T1ReleaseEvidenceError("external signoffs have the wrong contract")
    release = payload["release_revision"]
    if not isinstance(release, str) or not _RELEASE_PATTERN.fullmatch(release):
        raise T1ReleaseEvidenceError("external release revision is invalid")
    schema = payload["runtime_schema_revision"]
    if not isinstance(schema, str) or not schema:
        raise T1ReleaseEvidenceError("external runtime schema revision is invalid")
    generated_at = _parse_aware_datetime(payload["generated_at"], field_name="generated_at")
    signoffs = _mapping(payload["signoffs"], field_name="signoffs")
    if frozenset(signoffs) != REQUIRED_EXTERNAL_SIGNOFFS:
        raise T1ReleaseEvidenceError("every external signoff must appear exactly once")

    blocking: list[str] = []
    for name in sorted(REQUIRED_EXTERNAL_SIGNOFFS):
        signoff = _mapping(signoffs[name], field_name=name)
        if frozenset(signoff) != _SIGNOFF_FIELDS:
            raise T1ReleaseEvidenceError(f"{name} fields are invalid")
        status = signoff["status"]
        if status not in {"approved", "blocked"}:
            raise T1ReleaseEvidenceError(f"{name} status is invalid")
        reference = signoff["approval_reference"]
        if not isinstance(reference, str) or not _REFERENCE_PATTERN.fullmatch(reference):
            raise T1ReleaseEvidenceError(f"{name} approval reference is unsafe")
        approved_at = _parse_aware_datetime(signoff["approved_at"], field_name=f"{name}.approved_at")
        if approved_at > generated_at:
            raise T1ReleaseEvidenceError(f"{name} approval occurs after the manifest")
        review_due = signoff["review_due_on"]
        if not isinstance(review_due, str):
            raise T1ReleaseEvidenceError(f"{name} review due date is invalid")
        try:
            review_due_date = date.fromisoformat(review_due)
        except ValueError as error:
            raise T1ReleaseEvidenceError(f"{name} review due date is invalid") from error
        if review_due_date.isoformat() != review_due:
            raise T1ReleaseEvidenceError(f"{name} review due date is invalid")
        _sha256(signoff["evidence_sha256"], field_name=f"{name}.evidence_sha256")
        if status != "approved":
            blocking.append(f"{name}_not_approved")
        if review_due_date < generated_at.date():
            blocking.append(f"{name}_expired")
    return _ExternalSignoffs(
        release_revision=release,
        runtime_schema_revision=schema,
        blocking_codes=tuple(blocking),
    )


@dataclass(frozen=True, slots=True)
class T1ReleaseGateReport:
    status: str
    release_revision: str
    runtime_schema_revision: str
    checks: Mapping[str, str]
    blocking_codes: tuple[str, ...]
    input_sha256: Mapping[str, str]

    @property
    def counts_as_t1_release_evidence(self) -> bool:
        return self.status == "approved"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "scope": "market_morning_t1_release_gate",
            "status": self.status,
            "counts_as_t1_release_evidence": self.counts_as_t1_release_evidence,
            "release_revision": self.release_revision,
            "runtime_schema_revision": self.runtime_schema_revision,
            "checks": dict(self.checks),
            "blocking_codes": list(self.blocking_codes),
            "input_sha256": dict(self.input_sha256),
        }


def evaluate_t1_release_gate(
    *,
    mysql_acceptance: Mapping[str, Any],
    mysql_migration: Mapping[str, Any],
    staging_gate: Mapping[str, Any],
    external_signoffs: Mapping[str, Any],
) -> T1ReleaseGateReport:
    """Aggregate four independent evidence classes without echoing their contents."""

    acceptance = _mapping(mysql_acceptance, field_name="mysql_acceptance")
    migration = _mapping(mysql_migration, field_name="mysql_migration")
    staging = _mapping(staging_gate, field_name="staging_gate")
    signoffs_payload = _mapping(external_signoffs, field_name="external_signoffs")
    acceptance_passed, acceptance_target = _parse_mysql_manifest(
        acceptance,
        label="mysql_acceptance",
        scope="real_mysql_acceptance",
        scenario_ids=frozenset(item.scenario_id for item in MYSQL_ACCEPTANCE_SCENARIOS),
        destructive=False,
    )
    migration_passed, migration_target = _parse_mysql_manifest(
        migration,
        label="mysql_migration",
        scope="real_mysql_migration_rehearsal",
        scenario_ids=frozenset(item.scenario_id for item in MYSQL_MIGRATION_SCENARIOS),
        destructive=True,
    )
    staging_result = _parse_staging_gate(staging)
    signoffs = _parse_external_signoffs(signoffs_payload)

    blocking: list[str] = []
    if not acceptance_passed:
        blocking.append("mysql_acceptance_failed")
    if not migration_passed:
        blocking.append("mysql_migration_failed")
    if acceptance_target == migration_target:
        blocking.append("mysql_targets_not_isolated")
    if not staging_result.passed:
        blocking.append("staging_gate_not_ready")
    blocking.extend(signoffs.blocking_codes)
    if staging_result.release_revision != signoffs.release_revision:
        blocking.append("release_revision_mismatch")
    if signoffs.runtime_schema_revision != EXPECTED_MARKET_MORNING_SCHEMA_REVISION:
        blocking.append("runtime_schema_revision_mismatch")
    blocking_codes = tuple(dict.fromkeys(blocking))
    checks = {
        "external_signoffs": "failed" if signoffs.blocking_codes else "passed",
        "mysql_acceptance": "passed" if acceptance_passed else "failed",
        "mysql_migration": "passed" if migration_passed else "failed",
        "mysql_target_isolation": "passed" if acceptance_target != migration_target else "failed",
        "release_alignment": (
            "passed" if staging_result.release_revision == signoffs.release_revision else "failed"
        ),
        "runtime_schema": (
            "passed"
            if signoffs.runtime_schema_revision == EXPECTED_MARKET_MORNING_SCHEMA_REVISION
            else "failed"
        ),
        "staging_gate": "passed" if staging_result.passed else "failed",
    }
    return T1ReleaseGateReport(
        status="approved" if not blocking_codes else "not_ready",
        release_revision=signoffs.release_revision,
        runtime_schema_revision=signoffs.runtime_schema_revision,
        checks=checks,
        blocking_codes=blocking_codes,
        input_sha256={
            "external_signoffs": _canonical_sha256(signoffs_payload),
            "mysql_acceptance": _canonical_sha256(acceptance),
            "mysql_migration": _canonical_sha256(migration),
            "staging_gate": _canonical_sha256(staging),
        },
    )


__all__ = [
    "REQUIRED_EXTERNAL_SIGNOFFS",
    "T1ReleaseEvidenceError",
    "T1ReleaseGateReport",
    "evaluate_t1_release_gate",
]
