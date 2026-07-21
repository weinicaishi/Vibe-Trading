"""CLI for validating real Market Morning monitoring-drill evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.market_morning.monitoring_drill import (
    MonitoringDrillEvidenceError,
    parse_monitoring_drill_evidence,
)


_MAX_INPUT_BYTES = 128 * 1024


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a real staging warning/critical/resolved notification-chain drill and emit a privacy-safe evidence artifact."
        )
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > _MAX_INPUT_BYTES:
            raise MonitoringDrillEvidenceError(
                "monitoring drill input exceeds size limit"
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
    except MonitoringDrillEvidenceError:
        raise
    except Exception as error:
        raise MonitoringDrillEvidenceError(
            "monitoring drill input is unreadable"
        ) from error
    if not isinstance(payload, dict):
        raise MonitoringDrillEvidenceError(
            "monitoring drill input must be an object"
        )
    return payload


def _write_report(destination: Path, payload: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def run_cli(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.input.resolve() == args.output.resolve():
        parser.error("--output must not overwrite the source evidence file")
    try:
        evidence = parse_monitoring_drill_evidence(_read_payload(args.input))
    except MonitoringDrillEvidenceError as error:
        parser.error(str(error))
    _write_report(args.output, evidence.to_dict())
    return 0 if evidence.status == "passed" else 1


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
