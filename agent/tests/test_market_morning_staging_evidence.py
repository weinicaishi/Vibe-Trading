from __future__ import annotations

import json
from pathlib import Path

import pytest


REQUIRED_GATES = {
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


def _day(
    edition_date: str,
    *,
    previous_open: str,
    next_open: str,
    status: str = "passed",
    release_revision: str = "a" * 40,
    failure_codes: list[str] | None = None,
) -> dict:
    passed = status == "passed"
    gates = {name: "passed" for name in REQUIRED_GATES}
    if not passed:
        gates["publication"] = "failed"
    return {
        "schema_version": 1,
        "scope": "market_morning_staging_day",
        "environment_tier": "staging",
        "run_id": f"staging-{edition_date}",
        "release_revision": release_revision,
        "runtime_schema_revision": "0017_market_morning_content_reports",
        "edition_date": edition_date,
        "previous_jpx_open_date": previous_open,
        "next_jpx_open_date": next_open,
        "calendar_provider": "licensed-jpx-calendar",
        "provider_ids": [
            "licensed-tdnet",
            "licensed-edinet",
            "licensed-market-data",
            "production-email",
            "production-model",
        ],
        "started_at": f"{edition_date}T06:20:00+09:00",
        "finished_at": f"{edition_date}T07:05:00+09:00",
        "published_at": f"{edition_date}T06:58:00+09:00" if passed else None,
        "status": status,
        "counts_as_staging_day": passed,
        "contains_fixture_data": False,
        "contains_synthetic_data": False,
        "model_invocation_count": 3 if passed else 0,
        "gates": gates,
        "failure_codes": [] if passed else (failure_codes or ["publication_failed"]),
        "artifact_sha256": {name: "b" * 64 for name in REQUIRED_GATES},
    }


def _five_days(*, status_by_date: dict[str, str] | None = None) -> list[dict]:
    sequence = [
        ("2026-07-13", "2026-07-10", "2026-07-14"),
        ("2026-07-14", "2026-07-13", "2026-07-15"),
        ("2026-07-15", "2026-07-14", "2026-07-17"),
        ("2026-07-17", "2026-07-15", "2026-07-20"),
        ("2026-07-20", "2026-07-17", "2026-07-21"),
    ]
    status_by_date = status_by_date or {}
    return [
        _day(
            current,
            previous_open=previous,
            next_open=next_open,
            status=status_by_date.get(current, "passed"),
        )
        for current, previous, next_open in sequence
    ]


def test_staging_day_contract_accepts_only_complete_real_evidence() -> None:
    from src.market_morning.staging_evidence import parse_staging_day_evidence

    evidence = parse_staging_day_evidence(_five_days()[0])

    assert evidence.edition_date.isoformat() == "2026-07-13"
    assert evidence.status == "passed"
    assert evidence.counts_as_staging_day is True
    assert len(evidence.evidence_sha256) == 64
    assert "provider_ids" not in evidence.to_summary()
    assert "artifact_sha256" not in evidence.to_summary()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload.update(scope="automated_local_synthetic"), "scope"),
        (lambda payload: payload.update(environment_tier="local"), "environment"),
        (lambda payload: payload.update(calendar_provider="fixture_jpx"), "fixture"),
        (lambda payload: payload.update(contains_fixture_data=True), "fixture"),
        (lambda payload: payload.update(contains_synthetic_data=True), "synthetic"),
        (lambda payload: payload.update(provider_ids=["licensed-one"]), "at least five"),
        (lambda payload: payload.update(counts_as_staging_day=False), "counts_as_staging_day"),
        (lambda payload: payload.update(model_invocation_count=0), "model_invocation_count"),
        (lambda payload: payload.update(published_at="2026-07-13T06:29:00+09:00"), "06:30"),
        (lambda payload: payload.update(published_at="2026-07-13T08:31:00+09:00"), "08:30"),
        (lambda payload: payload.update(secret="must-not-be-accepted"), "unexpected fields"),
    ],
)
def test_staging_day_contract_rejects_non_t1_or_unsafe_evidence(
    mutation,
    message: str,
) -> None:
    from src.market_morning.staging_evidence import (
        StagingEvidenceError,
        parse_staging_day_evidence,
    )

    payload = _five_days()[0]
    mutation(payload)

    with pytest.raises(StagingEvidenceError, match=message):
        parse_staging_day_evidence(payload)


def test_failed_real_staging_day_is_auditable_but_never_counts() -> None:
    from src.market_morning.staging_evidence import parse_staging_day_evidence

    payload = _day(
        "2026-07-13",
        previous_open="2026-07-10",
        next_open="2026-07-14",
        status="failed",
        failure_codes=["market_snapshot_missing"],
    )
    evidence = parse_staging_day_evidence(payload)

    assert evidence.status == "failed"
    assert evidence.counts_as_staging_day is False
    assert evidence.failure_codes == ("market_snapshot_missing",)


def test_five_linked_jpx_trading_days_pass_the_t1_gate() -> None:
    from src.market_morning.staging_evidence import (
        evaluate_t1_staging_gate,
        parse_staging_day_evidence,
    )

    report = evaluate_t1_staging_gate(tuple(parse_staging_day_evidence(payload) for payload in _five_days()))
    result = report.to_dict()

    assert result["status"] == "passed"
    assert result["counts_as_t1_evidence"] is True
    assert result["required_consecutive_days"] == 5
    assert result["trailing_consecutive_days"] == 5
    assert result["eligible_dates"] == [
        "2026-07-13",
        "2026-07-14",
        "2026-07-15",
        "2026-07-17",
        "2026-07-20",
    ]
    assert "provider_ids" not in json.dumps(result)
    assert "artifact_sha256" not in json.dumps(result)


def test_failure_missing_day_or_release_change_resets_the_trailing_streak() -> None:
    from src.market_morning.staging_evidence import (
        evaluate_t1_staging_gate,
        parse_staging_day_evidence,
    )

    failed = _five_days(status_by_date={"2026-07-15": "failed"})
    failed_report = evaluate_t1_staging_gate(tuple(parse_staging_day_evidence(payload) for payload in failed))
    assert failed_report.status == "not_ready"
    assert failed_report.trailing_consecutive_days == 2
    assert "staging_day_failed" in failed_report.blocking_codes

    missing = _five_days()
    missing.pop(2)
    missing_report = evaluate_t1_staging_gate(tuple(parse_staging_day_evidence(payload) for payload in missing))
    assert missing_report.status == "not_ready"
    assert missing_report.trailing_consecutive_days == 2
    assert "jpx_session_gap" in missing_report.blocking_codes

    changed = _five_days()
    changed[-1]["release_revision"] = "c" * 40
    changed_report = evaluate_t1_staging_gate(tuple(parse_staging_day_evidence(payload) for payload in changed))
    assert changed_report.status == "not_ready"
    assert changed_report.trailing_consecutive_days == 1
    assert "release_changed" in changed_report.blocking_codes


def test_gate_requires_latest_days_to_pass_and_rejects_duplicates() -> None:
    from src.market_morning.staging_evidence import (
        StagingEvidenceError,
        evaluate_t1_staging_gate,
        parse_staging_day_evidence,
    )

    payloads = _five_days()
    payloads.append(
        _day(
            "2026-07-21",
            previous_open="2026-07-20",
            next_open="2026-07-22",
            status="failed",
        )
    )
    report = evaluate_t1_staging_gate(tuple(parse_staging_day_evidence(payload) for payload in payloads))
    assert report.longest_consecutive_days == 5
    assert report.trailing_consecutive_days == 0
    assert report.status == "not_ready"

    duplicate = parse_staging_day_evidence(_five_days()[0])
    with pytest.raises(StagingEvidenceError, match="duplicate edition_date"):
        evaluate_t1_staging_gate((duplicate, duplicate))


def test_staging_gate_cli_reads_strict_inputs_and_writes_hash_only_report(
    tmp_path: Path,
) -> None:
    from src.market_morning.staging_evidence_cli import run_cli

    inputs = []
    for index, payload in enumerate(_five_days()):
        path = tmp_path / f"day-{index}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        inputs.extend(["--input", str(path)])
    destination = tmp_path / "gate.json"

    exit_code = run_cli([*inputs, "--output", str(destination)])
    raw = destination.read_text(encoding="utf-8")
    result = json.loads(raw)

    assert exit_code == 0
    assert result["status"] == "passed"
    assert result["counts_as_t1_evidence"] is True
    assert "licensed-tdnet" not in raw
    assert "artifact_sha256" not in raw
    assert "secret" not in raw.lower()


def test_staging_gate_cli_fails_closed_for_t0_manifest(tmp_path: Path) -> None:
    from src.market_morning.staging_evidence_cli import run_cli

    source = tmp_path / "t0.json"
    source.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scope": "automated_local_synthetic",
                "counts_as_staging_day": False,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit):
        run_cli(["--input", str(source), "--output", str(tmp_path / "out.json")])


def test_pyproject_exposes_staging_evidence_gate_entrypoint() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")

    assert 'vibe-trading-market-morning-staging-gate = "src.market_morning.staging_evidence_cli:main"' in pyproject
