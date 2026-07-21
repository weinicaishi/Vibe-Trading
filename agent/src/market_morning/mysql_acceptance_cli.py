"""CLI for real-MySQL Market Morning acceptance evidence."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Protocol
from uuid import uuid4

from src.market_morning.mysql_acceptance import (
    AcceptanceTargetError,
    run_mysql_acceptance,
    validate_acceptance_database_url,
)
from src.market_morning.rehearsal import ProbeOutcome, RehearsalScenario


class MysqlProbeExecutor(Protocol):
    def __call__(
        self,
        scenario: RehearsalScenario,
        child_environment: Mapping[str, str],
    ) -> ProbeOutcome: ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _pytest_executor(
    scenario: RehearsalScenario,
    child_environment: Mapping[str, str],
) -> ProbeOutcome:
    started = time.monotonic()
    completed = subprocess.run(  # noqa: S603 - fixed executable and catalog-owned arguments
        [sys.executable, "-m", "pytest", "-q", *scenario.pytest_node_ids],
        check=False,
        cwd=Path.cwd(),
        env=dict(child_environment),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return ProbeOutcome(
        exit_code=completed.returncode,
        duration_ms=round((time.monotonic() - started) * 1_000),
        output_sha256=hashlib.sha256(completed.stdout).hexdigest(),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run write-capable probes against an explicitly dedicated Market "
            "Morning acceptance database and emit hash-only evidence."
        )
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--environment", required=True)
    parser.add_argument(
        "--confirm-write-to-dedicated-database",
        action="store_true",
        help="Required acknowledgement that the target is disposable acceptance infrastructure.",
    )
    return parser


def _write_manifest(destination: Path, payload: dict) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def run_cli(
    argv: list[str] | None = None,
    *,
    executor: MysqlProbeExecutor | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.confirm_write_to_dedicated_database:
        parser.error("explicit dedicated-database write confirmation is required")

    parent_environment = dict(os.environ if environ is None else environ)
    raw_url = parent_environment.get(
        "VIBE_MARKET_MORNING_ACCEPTANCE_DATABASE_URL",
        "",
    )
    try:
        target = validate_acceptance_database_url(
            raw_url,
            production_url=parent_environment.get(
                "VIBE_MARKET_MORNING_DATABASE_URL",
                "",
            ),
        )
    except AcceptanceTargetError as error:
        parser.error(str(error))

    child_environment = dict(parent_environment)
    child_environment.update(
        {
            "VIBE_MARKET_MORNING_DATABASE_URL": raw_url,
            "VIBE_MARKET_MORNING_ENABLED": "true",
            "VIBE_MARKET_MORNING_MYSQL_ACCEPTANCE_CONFIRMED": "true",
        }
    )
    probe = executor or _pytest_executor
    started_at = _utc_now()
    manifest = run_mysql_acceptance(
        lambda scenario: probe(scenario, child_environment),
        environment=args.environment,
        run_id=f"mysql-acceptance-{uuid4()}",
        target_fingerprint=target.target_fingerprint,
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
