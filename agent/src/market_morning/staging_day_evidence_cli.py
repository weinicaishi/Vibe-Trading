"""CLI for building one Market Morning staging-day evidence manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.market_morning.release_candidate import (
    ReleaseCandidateError,
    parse_release_candidate_manifest,
)
from src.market_morning.staging_day_evidence import (
    build_staging_day_evidence,
    parse_staging_gate_artifact,
)
from src.market_morning.staging_evidence import (
    REQUIRED_STAGING_GATES,
    StagingEvidenceError,
)


_MAX_INPUT_BYTES = 256 * 1024


def _gate_argument(value: str) -> tuple[str, Path]:
    gate, separator, raw_path = value.partition("=")
    if not separator or gate not in REQUIRED_STAGING_GATES or not raw_path.strip():
        raise argparse.ArgumentTypeError("--gate must use one required GATE=PATH pair")
    return gate, Path(raw_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Bind eleven real staging gate artifacts to one passed immutable Market Morning release."
        )
    )
    parser.add_argument("--release-candidate", required=True, type=Path)
    parser.add_argument(
        "--gate",
        action="append",
        required=True,
        type=_gate_argument,
        metavar="GATE=PATH",
    )
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise StagingEvidenceError("staging evidence input must be a regular file")
        if path.stat().st_size <= 0 or path.stat().st_size > _MAX_INPUT_BYTES:
            raise StagingEvidenceError("staging evidence input exceeds size limit")
        value = json.loads(path.read_text(encoding="utf-8"))
    except StagingEvidenceError:
        raise
    except Exception as error:
        raise StagingEvidenceError("staging evidence input is unreadable") from error
    if not isinstance(value, dict):
        raise StagingEvidenceError("staging evidence input must be an object")
    return value


def _write_payload(destination: Path, payload: dict[str, Any]) -> None:
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
    gate_pairs: list[tuple[str, Path]] = args.gate
    names = [name for name, _ in gate_pairs]
    if len(names) != len(set(names)):
        parser.error("duplicate --gate names are not allowed")
    paths = {name: path.resolve() for name, path in gate_pairs}
    if frozenset(paths) != REQUIRED_STAGING_GATES:
        parser.error("every required --gate name must be supplied exactly once")
    release_path = args.release_candidate.resolve()
    input_paths = (release_path, *paths.values())
    if len(input_paths) != len(set(input_paths)):
        parser.error("release candidate and gate paths must be distinct")
    output = args.output.resolve()
    if output in input_paths:
        parser.error("--output must not overwrite an input artifact")

    try:
        release_candidate = parse_release_candidate_manifest(
            _read_payload(release_path)
        )
        artifacts = {
            name: parse_staging_gate_artifact(_read_payload(path))
            for name, path in paths.items()
        }
        payload = build_staging_day_evidence(
            release_candidate=release_candidate,
            artifacts=artifacts,
        )
    except (ReleaseCandidateError, StagingEvidenceError) as error:
        parser.error(str(error))
    _write_payload(output, payload)
    return 0 if payload["status"] == "passed" else 1


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
