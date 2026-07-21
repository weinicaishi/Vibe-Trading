from __future__ import annotations

import json
from pathlib import Path

import pytest


REQUIRED_SCENARIOS = {
    "standard_trading_day",
    "us_holiday",
    "japan_holiday",
    "single_source_failure",
    "market_data_failure",
    "model_failure",
    "worker_restart",
    "email_timeout",
    "event_brief_rejection",
    "manual_publication_halt",
    "invite_and_suspension",
    "export_and_deletion",
}


def test_rehearsal_catalog_covers_runbook_matrix_without_claiming_staging() -> None:
    from src.market_morning.rehearsal import LOCAL_SYNTHETIC_SCENARIOS

    assert {scenario.scenario_id for scenario in LOCAL_SYNTHETIC_SCENARIOS} == REQUIRED_SCENARIOS
    assert len(LOCAL_SYNTHETIC_SCENARIOS) == len(REQUIRED_SCENARIOS)
    assert all(scenario.expected.strip() for scenario in LOCAL_SYNTHETIC_SCENARIOS)
    assert all(scenario.pytest_node_ids for scenario in LOCAL_SYNTHETIC_SCENARIOS)
    assert all(
        node_id.startswith("agent/tests/test_market_morning_")
        for scenario in LOCAL_SYNTHETIC_SCENARIOS
        for node_id in scenario.pytest_node_ids
    )


def test_rehearsal_runs_every_scenario_and_emits_hash_only_evidence() -> None:
    from src.market_morning.rehearsal import ProbeOutcome, run_local_synthetic_rehearsal

    called: list[str] = []

    def execute(scenario):
        called.append(scenario.scenario_id)
        return ProbeOutcome(
            exit_code=0,
            duration_ms=12,
            output_sha256="a" * 64,
        )

    manifest = run_local_synthetic_rehearsal(
        execute,
        environment="developer-laptop",
        run_id="rehearsal-001",
        started_at="2026-07-21T01:00:00Z",
        finished_at="2026-07-21T01:01:00Z",
    )
    payload = manifest.to_dict()

    assert called == [result["scenario_id"] for result in payload["scenarios"]]
    assert payload["status"] == "passed"
    assert payload["scope"] == "automated_local_synthetic"
    assert payload["counts_as_staging_day"] is False
    assert payload["summary"] == {"passed": 12, "failed": 0, "errors": 0, "total": 12}
    assert payload["schema_version"] == 1
    assert "stdout" not in json.dumps(payload).lower()
    assert "stderr" not in json.dumps(payload).lower()


def test_rehearsal_contains_probe_failures_and_continues_remaining_scenarios() -> None:
    from src.market_morning.rehearsal import ProbeOutcome, run_local_synthetic_rehearsal

    called: list[str] = []

    def execute(scenario):
        called.append(scenario.scenario_id)
        if scenario.scenario_id == "market_data_failure":
            return ProbeOutcome(exit_code=1, duration_ms=5, output_sha256="b" * 64)
        if scenario.scenario_id == "model_failure":
            raise RuntimeError("secret provider response")
        return ProbeOutcome(exit_code=0, duration_ms=5, output_sha256="c" * 64)

    manifest = run_local_synthetic_rehearsal(
        execute,
        environment="ci",
        run_id="rehearsal-002",
        started_at="2026-07-21T02:00:00Z",
        finished_at="2026-07-21T02:01:00Z",
    )
    payload = manifest.to_dict()

    assert len(called) == 12
    assert payload["status"] == "failed"
    assert payload["summary"] == {"passed": 10, "failed": 1, "errors": 1, "total": 12}
    failed = {item["scenario_id"]: item for item in payload["scenarios"]}
    assert failed["market_data_failure"]["failure_code"] == "probe_nonzero_exit"
    assert failed["model_failure"]["failure_code"] == "probe_execution_error"
    assert "secret provider response" not in json.dumps(payload)


@pytest.mark.parametrize("digest", ["short", "g" * 64, "A" * 64])
def test_probe_outcome_rejects_invalid_sha256(digest: str) -> None:
    from src.market_morning.rehearsal import ProbeOutcome

    with pytest.raises(ValueError, match="output_sha256"):
        ProbeOutcome(exit_code=0, duration_ms=1, output_sha256=digest)


def test_rehearsal_cli_writes_a_valid_manifest_without_raw_test_output(tmp_path: Path) -> None:
    from src.market_morning.rehearsal import ProbeOutcome
    from src.market_morning.rehearsal_cli import run_cli

    destination = tmp_path / "evidence" / "t0.json"

    def execute(_scenario):
        return ProbeOutcome(exit_code=0, duration_ms=3, output_sha256="d" * 64)

    exit_code = run_cli(
        ["--output", str(destination), "--environment", "local-test"],
        executor=execute,
    )

    assert exit_code == 0
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["status"] == "passed"
    assert payload["environment"] == "local-test"
    assert payload["counts_as_staging_day"] is False
    assert payload["summary"]["total"] == 12
    assert "stdout" not in destination.read_text(encoding="utf-8").lower()
    assert "stderr" not in destination.read_text(encoding="utf-8").lower()


def test_rehearsal_cli_records_finished_at_only_after_all_probes(
    tmp_path: Path, monkeypatch
) -> None:
    from src.market_morning import rehearsal_cli
    from src.market_morning.rehearsal import ProbeOutcome

    execution_started = False
    timestamps = iter(("2026-07-21T03:00:00Z", "2026-07-21T03:05:00Z"))

    def now() -> str:
        value = next(timestamps)
        if value.endswith("03:05:00Z"):
            assert execution_started is True
        return value

    def execute(_scenario):
        nonlocal execution_started
        execution_started = True
        return ProbeOutcome(exit_code=0, duration_ms=3, output_sha256="e" * 64)

    monkeypatch.setattr(rehearsal_cli, "_utc_now", now)
    destination = tmp_path / "timed.json"

    exit_code = rehearsal_cli.run_cli(
        ["--output", str(destination)],
        executor=execute,
    )

    assert exit_code == 0
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["started_at"] == "2026-07-21T03:00:00Z"
    assert payload["finished_at"] == "2026-07-21T03:05:00Z"


def test_pyproject_exposes_market_morning_rehearsal_entrypoint() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")

    assert (
        'vibe-trading-market-morning-rehearsal = "src.market_morning.rehearsal_cli:main"'
        in pyproject
    )
