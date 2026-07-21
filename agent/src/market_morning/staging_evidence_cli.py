"""CLI for evaluating strict Market Morning staging-day evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.market_morning.staging_evidence import (
    StagingEvidenceError,
    evaluate_t1_staging_gate,
    parse_staging_day_evidence,
)


_MAX_INPUT_BYTES = 256 * 1024


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate strict real-staging evidence for the latest five linked Market Morning JPX trading days."
        )
    )
    parser.add_argument("--input", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > _MAX_INPUT_BYTES:
            raise StagingEvidenceError("staging evidence input exceeds size limit")
        value = json.loads(path.read_text(encoding="utf-8"))
    except StagingEvidenceError:
        raise
    except Exception as error:
        raise StagingEvidenceError("staging evidence input is unreadable") from error
    if not isinstance(value, dict):
        raise StagingEvidenceError("staging evidence input must be an object")
    return value


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
    input_paths = tuple(path.resolve() for path in args.input)
    if len(input_paths) != len(set(input_paths)):
        parser.error("duplicate --input paths are not allowed")
    if args.output.resolve() in input_paths:
        parser.error("--output must not overwrite a source evidence file")
    try:
        evidence = tuple(parse_staging_day_evidence(_read_payload(path)) for path in input_paths)
        report = evaluate_t1_staging_gate(evidence)
    except StagingEvidenceError as error:
        parser.error(str(error))
    _write_report(args.output, report.to_dict())
    return 0 if report.status == "passed" else 1


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
