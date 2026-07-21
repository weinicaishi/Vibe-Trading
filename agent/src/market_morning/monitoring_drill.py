"""Strict, privacy-safe evidence contract for a real monitoring drill."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import re
from typing import Any


REQUIRED_MONITORING_DRILL_CHECKS = frozenset(
    {
        "alertmanager_route_loaded",
        "critical_firing_delivered",
        "critical_resolved_delivered",
        "dual_operator_acknowledged",
        "prometheus_rule_loaded",
        "silence_expiry_verified",
        "warning_firing_delivered",
        "warning_resolved_delivered",
    }
)

_EXPECTED_FIELDS = frozenset(
    {
        "schema_version",
        "scope",
        "environment_tier",
        "release_revision",
        "started_at",
        "finished_at",
        "rules_sha256",
        "alertmanager_config_sha256",
        "receiver_reference_sha256",
        "receiver_type",
        "operator_acknowledgement_count",
        "status",
        "counts_as_staging_observability_evidence",
        "contains_secret",
        "contains_sensitive_payload",
        "checks",
        "failure_codes",
        "artifact_sha256",
    }
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
_SAFE_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_FAILURE_CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")


class MonitoringDrillEvidenceError(ValueError):
    """Raised when a monitoring drill cannot count as staging evidence."""


def _aware_datetime(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise MonitoringDrillEvidenceError(f"{field_name} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise MonitoringDrillEvidenceError(
            f"{field_name} must be an ISO datetime"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MonitoringDrillEvidenceError(
            f"{field_name} must be timezone-aware"
        )
    return parsed


def _sha256(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise MonitoringDrillEvidenceError(
            f"{field_name} must be a lowercase SHA-256 digest"
        )
    return value


def _checks(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise MonitoringDrillEvidenceError("checks must be an object")
    if frozenset(value) != REQUIRED_MONITORING_DRILL_CHECKS:
        raise MonitoringDrillEvidenceError(
            "checks must contain every required monitoring drill check"
        )
    canonical: dict[str, str] = {}
    for name in sorted(REQUIRED_MONITORING_DRILL_CHECKS):
        status = value[name]
        if status not in {"passed", "failed"}:
            raise MonitoringDrillEvidenceError(
                "checks values must be passed or failed"
            )
        canonical[name] = status
    return canonical


def _artifact_hashes(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise MonitoringDrillEvidenceError("artifact_sha256 must be an object")
    if frozenset(value) != REQUIRED_MONITORING_DRILL_CHECKS:
        raise MonitoringDrillEvidenceError(
            "artifact_sha256 must contain every required monitoring drill check"
        )
    return {
        name: _sha256(value[name], field_name="artifact_sha256")
        for name in sorted(REQUIRED_MONITORING_DRILL_CHECKS)
    }


def _failure_codes(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) != len(set(value)):
        raise MonitoringDrillEvidenceError(
            "failure_codes must be a unique list"
        )
    codes: list[str] = []
    for code in value:
        if not isinstance(code, str) or not _FAILURE_CODE_PATTERN.fullmatch(code):
            raise MonitoringDrillEvidenceError(
                "failure_codes must contain stable lowercase codes"
            )
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
class MonitoringDrillEvidence:
    release_revision: str
    started_at: datetime
    finished_at: datetime
    rules_sha256: str
    alertmanager_config_sha256: str
    receiver_reference_sha256: str
    receiver_type: str
    operator_acknowledgement_count: int
    status: str
    counts_as_staging_observability_evidence: bool
    checks: Mapping[str, str]
    failure_codes: tuple[str, ...]
    evidence_sha256: str

    def to_dict(self) -> dict[str, Any]:
        """Return a normalized, secret-free artifact for the staging job."""

        return {
            "schema_version": 1,
            "scope": "market_morning_monitoring_drill_evidence",
            "environment_tier": "staging",
            "release_revision": self.release_revision,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "rules_sha256": self.rules_sha256,
            "alertmanager_config_sha256": self.alertmanager_config_sha256,
            "receiver_reference_sha256": self.receiver_reference_sha256,
            "receiver_type": self.receiver_type,
            "operator_acknowledgement_count": self.operator_acknowledgement_count,
            "status": self.status,
            "counts_as_staging_observability_evidence": (
                self.counts_as_staging_observability_evidence
            ),
            "checks": dict(self.checks),
            "failure_codes": list(self.failure_codes),
            "evidence_sha256": self.evidence_sha256,
        }


def parse_monitoring_drill_evidence(
    payload: Mapping[str, Any],
) -> MonitoringDrillEvidence:
    """Validate one real warning/critical/resolved notification-chain drill."""

    if not isinstance(payload, Mapping):
        raise MonitoringDrillEvidenceError("monitoring drill evidence must be an object")
    fields = frozenset(payload)
    if fields - _EXPECTED_FIELDS:
        raise MonitoringDrillEvidenceError(
            "monitoring drill evidence contains unexpected fields"
        )
    if _EXPECTED_FIELDS - fields:
        raise MonitoringDrillEvidenceError(
            "monitoring drill evidence is missing required fields"
        )
    if payload["schema_version"] != 1:
        raise MonitoringDrillEvidenceError("schema_version is unsupported")
    if payload["scope"] != "market_morning_monitoring_drill":
        raise MonitoringDrillEvidenceError("scope is not a monitoring drill")
    if payload["environment_tier"] != "staging":
        raise MonitoringDrillEvidenceError("environment_tier must be staging")

    release = payload["release_revision"]
    if not isinstance(release, str) or not _RELEASE_PATTERN.fullmatch(release):
        raise MonitoringDrillEvidenceError(
            "release_revision must be a lowercase immutable revision"
        )
    started_at = _aware_datetime(payload["started_at"], field_name="started_at")
    finished_at = _aware_datetime(payload["finished_at"], field_name="finished_at")
    if not started_at < finished_at:
        raise MonitoringDrillEvidenceError("finished_at must follow started_at")
    if finished_at.astimezone(started_at.tzinfo) - started_at > timedelta(hours=8):
        raise MonitoringDrillEvidenceError("monitoring drill must finish within eight hours")

    rules_sha256 = _sha256(payload["rules_sha256"], field_name="rules_sha256")
    alertmanager_sha256 = _sha256(
        payload["alertmanager_config_sha256"],
        field_name="alertmanager_config_sha256",
    )
    receiver_sha256 = _sha256(
        payload["receiver_reference_sha256"],
        field_name="receiver_reference_sha256",
    )
    receiver_type = payload["receiver_type"]
    if not isinstance(receiver_type, str) or not _SAFE_SLUG_PATTERN.fullmatch(
        receiver_type
    ):
        raise MonitoringDrillEvidenceError("receiver_type must be a safe slug")
    operator_count = payload["operator_acknowledgement_count"]
    if type(operator_count) is not int or not 0 <= operator_count <= 20:
        raise MonitoringDrillEvidenceError(
            "operator_acknowledgement_count must be between 0 and 20"
        )

    status = payload["status"]
    if status not in {"passed", "failed"}:
        raise MonitoringDrillEvidenceError("status must be passed or failed")
    counts = payload["counts_as_staging_observability_evidence"]
    contains_secret = payload["contains_secret"]
    contains_sensitive = payload["contains_sensitive_payload"]
    if type(counts) is not bool:
        raise MonitoringDrillEvidenceError(
            "counts_as_staging_observability_evidence must be boolean"
        )
    if type(contains_secret) is not bool or type(contains_sensitive) is not bool:
        raise MonitoringDrillEvidenceError(
            "privacy flags must be boolean"
        )
    if contains_secret or contains_sensitive:
        raise MonitoringDrillEvidenceError(
            "monitoring drill evidence must not contain secrets or sensitive payloads"
        )

    checks = _checks(payload["checks"])
    _artifact_hashes(payload["artifact_sha256"])
    failure_codes = _failure_codes(payload["failure_codes"])
    if status == "passed":
        if counts is not True:
            raise MonitoringDrillEvidenceError(
                "passed evidence must count as staging observability evidence"
            )
        if any(value != "passed" for value in checks.values()):
            raise MonitoringDrillEvidenceError(
                "passed evidence requires every check to pass"
            )
        if operator_count < 2:
            raise MonitoringDrillEvidenceError(
                "passed evidence requires two operator acknowledgements"
            )
        if failure_codes:
            raise MonitoringDrillEvidenceError(
                "passed evidence must not contain failure_codes"
            )
    else:
        if counts is not False:
            raise MonitoringDrillEvidenceError(
                "failed evidence cannot count as staging observability evidence"
            )
        if all(value == "passed" for value in checks.values()):
            raise MonitoringDrillEvidenceError(
                "failed evidence requires at least one failed check"
            )
        if not failure_codes:
            raise MonitoringDrillEvidenceError(
                "failed evidence requires failure_codes"
            )

    return MonitoringDrillEvidence(
        release_revision=release,
        started_at=started_at,
        finished_at=finished_at,
        rules_sha256=rules_sha256,
        alertmanager_config_sha256=alertmanager_sha256,
        receiver_reference_sha256=receiver_sha256,
        receiver_type=receiver_type,
        operator_acknowledgement_count=operator_count,
        status=status,
        counts_as_staging_observability_evidence=counts,
        checks=checks,
        failure_codes=failure_codes,
        evidence_sha256=_canonical_sha256(payload),
    )


__all__ = [
    "MonitoringDrillEvidence",
    "MonitoringDrillEvidenceError",
    "REQUIRED_MONITORING_DRILL_CHECKS",
    "parse_monitoring_drill_evidence",
]
