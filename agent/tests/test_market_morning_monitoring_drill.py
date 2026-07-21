from __future__ import annotations

import json
from pathlib import Path

import pytest


CHECKS = {
    "alertmanager_route_loaded",
    "critical_firing_delivered",
    "critical_resolved_delivered",
    "dual_operator_acknowledged",
    "prometheus_rule_loaded",
    "silence_expiry_verified",
    "warning_firing_delivered",
    "warning_resolved_delivered",
}
TEMPLATE_PATH = Path(
    "docs/evidence/market-morning/monitoring-drill.template.json"
)


def _payload(*, status: str = "passed") -> dict:
    passed = status == "passed"
    checks = {name: "passed" for name in CHECKS}
    if not passed:
        checks["critical_resolved_delivered"] = "failed"
    return {
        "schema_version": 1,
        "scope": "market_morning_monitoring_drill",
        "environment_tier": "staging",
        "release_revision": "a" * 40,
        "started_at": "2026-07-22T06:00:00+09:00",
        "finished_at": "2026-07-22T06:45:00+09:00",
        "rules_sha256": "b" * 64,
        "alertmanager_config_sha256": "c" * 64,
        "receiver_reference_sha256": "d" * 64,
        "receiver_type": "approved-webhook",
        "operator_acknowledgement_count": 2,
        "status": status,
        "counts_as_staging_observability_evidence": passed,
        "contains_secret": False,
        "contains_sensitive_payload": False,
        "checks": checks,
        "failure_codes": [] if passed else ["critical_resolved_missing"],
        "artifact_sha256": {name: "e" * 64 for name in CHECKS},
    }


def test_monitoring_drill_accepts_complete_real_notification_chain() -> None:
    from src.market_morning.monitoring_drill import (
        parse_monitoring_drill_evidence,
    )

    evidence = parse_monitoring_drill_evidence(_payload())
    result = evidence.to_dict()

    assert evidence.status == "passed"
    assert evidence.counts_as_staging_observability_evidence is True
    assert result["scope"] == "market_morning_monitoring_drill_evidence"
    assert len(result["evidence_sha256"]) == 64
    assert "artifact_sha256" not in result
    assert "contains_secret" not in result


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(environment_tier="local"), "environment_tier"),
        (lambda value: value.update(scope="synthetic_drill"), "scope"),
        (lambda value: value.update(contains_secret=True), "secrets"),
        (
            lambda value: value.update(contains_sensitive_payload=True),
            "sensitive payloads",
        ),
        (
            lambda value: value.update(operator_acknowledgement_count=1),
            "two operator",
        ),
        (
            lambda value: value["checks"].pop("warning_resolved_delivered"),
            "every required",
        ),
        (lambda value: value.update(webhook_url="secret"), "unexpected fields"),
    ],
)
def test_monitoring_drill_rejects_non_staging_partial_or_sensitive_evidence(
    mutation,
    message: str,
) -> None:
    from src.market_morning.monitoring_drill import (
        MonitoringDrillEvidenceError,
        parse_monitoring_drill_evidence,
    )

    payload = _payload()
    mutation(payload)
    with pytest.raises(MonitoringDrillEvidenceError, match=message):
        parse_monitoring_drill_evidence(payload)


def test_failed_drill_is_auditable_but_never_counts() -> None:
    from src.market_morning.monitoring_drill import (
        parse_monitoring_drill_evidence,
    )

    evidence = parse_monitoring_drill_evidence(_payload(status="failed"))

    assert evidence.status == "failed"
    assert evidence.counts_as_staging_observability_evidence is False
    assert evidence.failure_codes == ("critical_resolved_missing",)


def test_repository_template_is_valid_but_permanently_blocked() -> None:
    from src.market_morning.monitoring_drill import (
        parse_monitoring_drill_evidence,
    )

    payload = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    evidence = parse_monitoring_drill_evidence(payload)

    assert evidence.status == "failed"
    assert evidence.counts_as_staging_observability_evidence is False
    assert evidence.failure_codes == ("monitoring_drill_not_run",)


def test_monitoring_drill_cli_writes_normalized_hash_only_report(
    tmp_path: Path,
) -> None:
    from src.market_morning.monitoring_drill_cli import run_cli

    source = tmp_path / "source.json"
    destination = tmp_path / "report.json"
    source.write_text(json.dumps(_payload()), encoding="utf-8")

    assert run_cli(
        ["--input", str(source), "--output", str(destination)]
    ) == 0
    raw = destination.read_text(encoding="utf-8")
    result = json.loads(raw)

    assert result["status"] == "passed"
    assert result["counts_as_staging_observability_evidence"] is True
    assert "artifact_sha256" not in raw
    assert "webhook_url" not in raw
    assert "secret" not in raw.lower()


def test_monitoring_drill_cli_preserves_failed_status_and_nonzero_exit(
    tmp_path: Path,
) -> None:
    from src.market_morning.monitoring_drill_cli import run_cli

    source = tmp_path / "failed.json"
    destination = tmp_path / "failed-report.json"
    source.write_text(json.dumps(_payload(status="failed")), encoding="utf-8")

    assert run_cli(
        ["--input", str(source), "--output", str(destination)]
    ) == 1
    assert json.loads(destination.read_text(encoding="utf-8"))["status"] == "failed"
