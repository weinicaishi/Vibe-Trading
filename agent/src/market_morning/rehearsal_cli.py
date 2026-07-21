"""CLI for producing hash-only local Market Morning rehearsal evidence."""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

from src.market_morning.rehearsal import (
    ProbeOutcome,
    RehearsalScenario,
    ScenarioExecutor,
    run_local_synthetic_rehearsal,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _pytest_executor(scenario: RehearsalScenario) -> ProbeOutcome:
    started = time.monotonic()
    completed = subprocess.run(  # noqa: S603 - fixed executable and catalog-owned arguments
        [sys.executable, "-m", "pytest", "-q", *scenario.pytest_node_ids],
        check=False,
        cwd=Path.cwd(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    duration_ms = round((time.monotonic() - started) * 1_000)
    return ProbeOutcome(
        exit_code=completed.returncode,
        duration_ms=duration_ms,
        output_sha256=hashlib.sha256(completed.stdout).hexdigest(),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the 12 local synthetic Market Morning T0 probes and write a "
            "hash-only evidence manifest. This never counts as a staging day."
        )
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--environment", default="local")
    return parser


def _write_manifest(destination: Path, payload: dict) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def run_cli(argv: list[str] | None = None, *, executor: ScenarioExecutor | None = None) -> int:
    args = _parser().parse_args(argv)
    started_at = _utc_now()
    manifest = run_local_synthetic_rehearsal(
        executor or _pytest_executor,
        environment=args.environment,
        run_id=f"t0-{uuid4()}",
        started_at=started_at,
        finished_at=started_at,
    )
    manifest = replace(manifest, finished_at=_utc_now())
    _write_manifest(args.output, manifest.to_dict())
    return 0 if manifest.status == "passed" else 1


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
