"""CLI for generating immutable Market Morning release-candidate evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from src.market_morning.release_candidate import (
    REQUIRED_VERIFICATION_EVIDENCE,
    ReleaseCandidateError,
    collect_repository_snapshot,
    collect_verification_snapshot,
    evaluate_release_candidate,
)


def _evidence_argument(value: str) -> tuple[str, Path]:
    name, separator, raw_path = value.partition("=")
    if not separator or name not in REQUIRED_VERIFICATION_EVIDENCE or not raw_path.strip():
        raise argparse.ArgumentTypeError(
            "--evidence must use one required NAME=PATH pair"
        )
    return name, Path(raw_path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Bind a clean Market Morning source revision to privacy-safe hashes of required verification outputs."
        )
    )
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--environment", choices=("ci", "staging"), required=True)
    parser.add_argument("--expected-release-revision", required=True)
    parser.add_argument(
        "--evidence",
        action="append",
        required=True,
        type=_evidence_argument,
        metavar="NAME=PATH",
    )
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _write_manifest(destination: Path, payload: dict[str, object]) -> None:
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
    evidence_pairs: list[tuple[str, Path]] = args.evidence
    evidence_names = [name for name, _ in evidence_pairs]
    if len(evidence_names) != len(set(evidence_names)):
        parser.error("duplicate --evidence names are not allowed")
    evidence_paths = dict(evidence_pairs)
    if frozenset(evidence_paths) != frozenset(REQUIRED_VERIFICATION_EVIDENCE):
        parser.error("every required --evidence name must be supplied exactly once")

    repository_root = args.repository_root.resolve()
    output = args.output.resolve()
    if output == repository_root or repository_root in output.parents:
        parser.error("--output must be outside the repository to preserve a clean source tree")
    if output in {path.resolve() for path in evidence_paths.values()}:
        parser.error("--output must not overwrite verification evidence")

    try:
        repository = collect_repository_snapshot(repository_root)
        verification = collect_verification_snapshot(
            evidence_paths,
            environment_tier=args.environment,
        )
        manifest = evaluate_release_candidate(
            environment_tier=args.environment,
            expected_release_revision=args.expected_release_revision,
            repository=repository,
            verification=verification,
            generated_at=datetime.now(timezone.utc),
        )
    except ReleaseCandidateError as error:
        parser.error(str(error))
    _write_manifest(output, manifest.to_dict())
    return 0 if manifest.status == "passed" else 1


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
