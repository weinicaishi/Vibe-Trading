"""CLI for the aggregate Market Morning T1 release evidence gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.market_morning.t1_release_gate import (
    T1ReleaseEvidenceError,
    evaluate_t1_release_gate,
)


_MAX_INPUT_BYTES = 512 * 1024


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate real MySQL, migration, five-day staging and external signoff evidence before enabling Market Morning T1."
        )
    )
    parser.add_argument("--mysql-acceptance", required=True, type=Path)
    parser.add_argument("--mysql-migration", required=True, type=Path)
    parser.add_argument("--staging-gate", required=True, type=Path)
    parser.add_argument("--external-signoffs", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > _MAX_INPUT_BYTES:
            raise T1ReleaseEvidenceError("release evidence input exceeds size limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except T1ReleaseEvidenceError:
        raise
    except Exception as error:
        raise T1ReleaseEvidenceError("release evidence input is unreadable") from error
    if not isinstance(payload, dict):
        raise T1ReleaseEvidenceError("release evidence input must be an object")
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
    inputs = {
        "mysql_acceptance": args.mysql_acceptance.resolve(),
        "mysql_migration": args.mysql_migration.resolve(),
        "staging_gate": args.staging_gate.resolve(),
        "external_signoffs": args.external_signoffs.resolve(),
    }
    if len(set(inputs.values())) != len(inputs):
        parser.error("release evidence inputs must use distinct files")
    if args.output.resolve() in set(inputs.values()):
        parser.error("--output must not overwrite a source evidence file")
    try:
        report = evaluate_t1_release_gate(
            **{name: _read_payload(path) for name, path in inputs.items()}
        )
    except T1ReleaseEvidenceError as error:
        parser.error(str(error))
    _write_report(args.output, report.to_dict())
    return 0 if report.status == "approved" else 1


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
