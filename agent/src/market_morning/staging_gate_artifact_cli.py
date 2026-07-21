"""CLI for wrapping one real staging probe in a strict hash-only envelope."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.market_morning.release_candidate import (
    ReleaseCandidateError,
    parse_release_candidate_manifest,
)
from src.market_morning.staging_day_evidence import (
    build_staging_gate_artifact_payload,
)
from src.market_morning.staging_evidence import (
    REQUIRED_STAGING_GATES,
    StagingEvidenceError,
)


_MAX_JSON_BYTES = 256 * 1024
_MAX_EVIDENCE_BYTES = 64 * 1024 * 1024


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Hash one real Market Morning staging probe and bind it to an immutable release candidate."
        )
    )
    parser.add_argument("--release-candidate", required=True, type=Path)
    parser.add_argument("--gate", required=True, choices=tuple(sorted(REQUIRED_STAGING_GATES)))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--edition-date", required=True)
    parser.add_argument("--previous-jpx-open-date", required=True)
    parser.add_argument("--next-jpx-open-date", required=True)
    parser.add_argument("--calendar-provider", required=True)
    parser.add_argument("--provider-id", action="append", default=[])
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--finished-at", required=True)
    parser.add_argument("--published-at")
    parser.add_argument("--status", required=True, choices=("passed", "failed"))
    parser.add_argument("--model-invocation-count", type=int, default=0)
    parser.add_argument("--failure-code")
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise StagingEvidenceError("release candidate must be a regular file")
        size = path.stat().st_size
        if size <= 0 or size > _MAX_JSON_BYTES:
            raise StagingEvidenceError("release candidate exceeds size limit")
        value = json.loads(path.read_text(encoding="utf-8"))
    except StagingEvidenceError:
        raise
    except Exception as error:
        raise StagingEvidenceError("release candidate is unreadable") from error
    if not isinstance(value, dict):
        raise StagingEvidenceError("release candidate must be an object")
    return value


def _hash_evidence(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            raise StagingEvidenceError("gate evidence must be a regular file")
        size = path.stat().st_size
        if size <= 0 or size > _MAX_EVIDENCE_BYTES:
            raise StagingEvidenceError("gate evidence exceeds size limit")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    except StagingEvidenceError:
        raise
    except OSError as error:
        raise StagingEvidenceError("gate evidence is unreadable") from error
    return digest.hexdigest()


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
    release_path = args.release_candidate.resolve()
    evidence_path = args.evidence.resolve()
    output = args.output.resolve()
    if release_path == evidence_path:
        parser.error("release candidate and gate evidence must be distinct")
    if output in {release_path, evidence_path}:
        parser.error("--output must not overwrite an input")
    if len(args.provider_id) != len(set(args.provider_id)):
        parser.error("duplicate --provider-id values are not allowed")

    try:
        release_candidate = parse_release_candidate_manifest(
            _read_json(release_path)
        )
        payload = build_staging_gate_artifact_payload(
            release_candidate=release_candidate,
            gate=args.gate,
            run_id=args.run_id,
            edition_date=args.edition_date,
            previous_jpx_open_date=args.previous_jpx_open_date,
            next_jpx_open_date=args.next_jpx_open_date,
            calendar_provider=args.calendar_provider,
            provider_ids=tuple(args.provider_id),
            started_at=args.started_at,
            finished_at=args.finished_at,
            published_at=args.published_at,
            status=args.status,
            model_invocation_count=args.model_invocation_count,
            failure_code=args.failure_code,
            evidence_sha256=_hash_evidence(evidence_path),
        )
    except (ReleaseCandidateError, StagingEvidenceError) as error:
        parser.error(str(error))
    _write_payload(output, payload)
    return 0 if payload["status"] == "passed" else 1


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
