"""Fail-closed, privacy-safe Market Morning release-candidate manifests."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from src.market_morning.db import EXPECTED_MARKET_MORNING_SCHEMA_REVISION


REQUIRED_SOURCE_ARTIFACTS: Mapping[str, str] = {
    "backend_dependency_lock": "requirements-lock.txt",
    "container_image_definition": "Dockerfile",
    "frontend_dependency_lock": "frontend/package-lock.json",
    "market_morning_ci_workflow": ".github/workflows/test.yml",
    "market_morning_auth0_post_login_action": "deploy/auth0/market-morning-post-login.js",
    "market_morning_auth0_runbook": "deploy/auth0/README.md",
    "market_morning_migration_head": ("agent/migrations/market_morning/versions/0018_market_morning_auth_sessions.py"),
    "market_morning_mysql_bundle": ("database/market-morning/mysql/market_morning_mysql_0018.zip"),
    "market_morning_mysql_schema": ("database/market-morning/mysql/market_morning_schema_0018.sql"),
    "market_morning_monitoring_rules": ("deploy/market-morning/monitoring/market-morning.rules.yml"),
    "market_morning_monitoring_drill_template": ("docs/evidence/market-morning/monitoring-drill.template.json"),
    "market_morning_operations_runbook": ("docs/market-morning-operations-runbook.md"),
    "market_morning_production_runtime_factory": ("agent/src/market_morning/production_runtime_factory.py"),
    "market_morning_t1_signoff_template": ("docs/evidence/market-morning/t1-external-signoffs.template.json"),
    "python_project_contract": "pyproject.toml",
    "runtime_compose_definition": "docker-compose.yml",
}
REQUIRED_VERIFICATION_EVIDENCE = (
    "backend_tests",
    "frontend_build",
    "frontend_tests",
    "mysql_acceptance",
    "mysql_migration",
)
RELEASE_CANDIDATE_CHECKS = frozenset(
    {
        "artifacts_tracked",
        "clean_worktree",
        "evidence_hashes_complete",
        "evidence_valid",
        "immutable_revision",
        "release_revision_matches",
        "repository_readable",
        "runtime_schema_current",
        "source_hashes_complete",
    }
)
_BLOCKING_CODE_BY_CHECK = {
    "artifacts_tracked": "source_artifacts_not_tracked",
    "clean_worktree": "worktree_not_clean",
    "evidence_hashes_complete": "verification_evidence_missing",
    "evidence_valid": "verification_evidence_invalid",
    "immutable_revision": "release_revision_unavailable",
    "release_revision_matches": "release_revision_mismatch",
    "repository_readable": "repository_unreadable",
    "runtime_schema_current": "runtime_schema_not_current",
    "source_hashes_complete": "source_artifacts_unreadable",
}
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "scope",
        "environment_tier",
        "generated_at",
        "expected_release_revision",
        "release_revision",
        "runtime_schema_revision",
        "status",
        "counts_as_staging_day",
        "counts_as_t1_release_evidence",
        "checks",
        "blocking_codes",
        "source_artifact_sha256",
        "verification_evidence_sha256",
        "manifest_sha256",
    }
)

_RELEASE_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_SOURCE_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_VERIFICATION_EVIDENCE_BYTES = 16 * 1024 * 1024


class ReleaseCandidateError(ValueError):
    """Raised for unsafe or malformed release-candidate inputs."""


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    release_revision: str | None
    repository_readable: bool
    worktree_clean: bool
    source_artifact_sha256: Mapping[str, str | None]
    tracked_source_artifacts: frozenset[str]


@dataclass(frozen=True, slots=True)
class VerificationSnapshot:
    evidence_sha256: Mapping[str, str | None]
    valid_evidence: frozenset[str]


@dataclass(frozen=True, slots=True)
class ReleaseCandidateManifest:
    payload: Mapping[str, Any]

    @property
    def status(self) -> str:
        return str(self.payload["status"])

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(dict(self.payload))

    @property
    def release_revision(self) -> str | None:
        value = self.payload["release_revision"]
        return value if isinstance(value, str) else None

    @property
    def manifest_sha256(self) -> str:
        return str(self.payload["manifest_sha256"])


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_hash_map(
    value: Any,
    *,
    required_names: frozenset[str],
    allow_missing: bool,
) -> dict[str, str | None]:
    if not isinstance(value, Mapping) or frozenset(value) != required_names:
        raise ReleaseCandidateError("release candidate hash map is incomplete")
    parsed: dict[str, str | None] = {}
    for name in sorted(required_names):
        digest = value[name]
        if digest is None and allow_missing:
            parsed[name] = None
            continue
        if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
            raise ReleaseCandidateError("release candidate hash is invalid")
        parsed[name] = digest
    return parsed


def parse_release_candidate_manifest(payload: Mapping[str, Any]) -> ReleaseCandidateManifest:
    """Verify a release-candidate manifest before another gate consumes it."""

    if not isinstance(payload, Mapping) or frozenset(payload) != _MANIFEST_FIELDS:
        raise ReleaseCandidateError("release candidate fields are incomplete or unexpected")
    if payload["schema_version"] != 1:
        raise ReleaseCandidateError("release candidate schema is unsupported")
    if payload["scope"] != "market_morning_release_candidate":
        raise ReleaseCandidateError("release candidate scope is invalid")
    if payload["environment_tier"] not in {"ci", "staging"}:
        raise ReleaseCandidateError("release candidate environment is invalid")
    try:
        generated_at = datetime.fromisoformat(payload["generated_at"])
    except (TypeError, ValueError):
        raise ReleaseCandidateError("release candidate generated_at is invalid") from None
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ReleaseCandidateError("release candidate generated_at is invalid")

    expected_revision = payload["expected_release_revision"]
    if not isinstance(expected_revision, str) or not _RELEASE_PATTERN.fullmatch(expected_revision):
        raise ReleaseCandidateError("release candidate expected revision is invalid")
    release_revision = payload["release_revision"]
    if release_revision is not None and (
        not isinstance(release_revision, str) or not _RELEASE_PATTERN.fullmatch(release_revision)
    ):
        raise ReleaseCandidateError("release candidate revision is invalid")
    if payload["runtime_schema_revision"] != EXPECTED_MARKET_MORNING_SCHEMA_REVISION:
        raise ReleaseCandidateError("release candidate runtime schema is not current")
    if payload["counts_as_staging_day"] is not False:
        raise ReleaseCandidateError("release candidate cannot count as a staging day")
    if payload["counts_as_t1_release_evidence"] is not False:
        raise ReleaseCandidateError("release candidate cannot count as T1 release evidence")

    checks_value = payload["checks"]
    if not isinstance(checks_value, Mapping) or frozenset(checks_value) != RELEASE_CANDIDATE_CHECKS:
        raise ReleaseCandidateError("release candidate checks are incomplete")
    checks: dict[str, str] = {}
    for name in sorted(RELEASE_CANDIDATE_CHECKS):
        state = checks_value[name]
        if state not in {"passed", "failed"}:
            raise ReleaseCandidateError("release candidate check state is invalid")
        checks[name] = state

    status = payload["status"]
    if status not in {"passed", "blocked"}:
        raise ReleaseCandidateError("release candidate status is invalid")
    blocking_codes = payload["blocking_codes"]
    if not isinstance(blocking_codes, list) or len(blocking_codes) != len(set(blocking_codes)):
        raise ReleaseCandidateError("release candidate blocking codes are invalid")
    expected_blocking_codes = [
        _BLOCKING_CODE_BY_CHECK[name] for name in sorted(RELEASE_CANDIDATE_CHECKS) if checks[name] == "failed"
    ]
    if blocking_codes != expected_blocking_codes:
        raise ReleaseCandidateError("release candidate blocking codes do not match checks")
    if status == "passed":
        if expected_blocking_codes or release_revision != expected_revision:
            raise ReleaseCandidateError("passed release candidate is internally inconsistent")
    elif not expected_blocking_codes:
        raise ReleaseCandidateError("blocked release candidate requires a failed check")

    _parse_hash_map(
        payload["source_artifact_sha256"],
        required_names=frozenset(REQUIRED_SOURCE_ARTIFACTS),
        allow_missing=status == "blocked",
    )
    _parse_hash_map(
        payload["verification_evidence_sha256"],
        required_names=frozenset(REQUIRED_VERIFICATION_EVIDENCE),
        allow_missing=status == "blocked",
    )
    manifest_sha256 = payload["manifest_sha256"]
    if not isinstance(manifest_sha256, str) or not _SHA256_PATTERN.fullmatch(manifest_sha256):
        raise ReleaseCandidateError("release candidate manifest hash is invalid")
    canonical = dict(payload)
    canonical.pop("manifest_sha256")
    if _canonical_sha256(canonical) != manifest_sha256:
        raise ReleaseCandidateError("release candidate manifest hash does not match")
    return ReleaseCandidateManifest(payload=deepcopy(dict(payload)))


def _run_git(repository_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", "-C", str(repository_root), *arguments),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _sha256_file(path: Path, *, maximum_bytes: int) -> str:
    if path.is_symlink() or not path.is_file():
        raise ReleaseCandidateError("release evidence must be a regular non-symlink file")
    size = path.stat().st_size
    if size <= 0 or size > maximum_bytes:
        raise ReleaseCandidateError("release evidence file size is outside the safe limit")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def collect_repository_snapshot(repository_root: Path) -> RepositorySnapshot:
    """Read immutable source facts without retaining paths or Git status text."""

    root = repository_root.resolve()
    revision: str | None = None
    repository_readable = True
    worktree_clean = False
    tracked: set[str] = set()
    artifact_hashes: dict[str, str | None] = {name: None for name in REQUIRED_SOURCE_ARTIFACTS}

    try:
        top_level = _run_git(root, "rev-parse", "--show-toplevel")
        head = _run_git(root, "rev-parse", "HEAD")
        status = _run_git(root, "status", "--porcelain=v1", "--untracked-files=all")
    except (OSError, subprocess.SubprocessError):
        repository_readable = False
    else:
        if (
            top_level.returncode != 0
            or Path(top_level.stdout.strip()).resolve() != root
            or head.returncode != 0
            or status.returncode != 0
        ):
            repository_readable = False
        else:
            candidate_revision = head.stdout.strip()
            if _RELEASE_PATTERN.fullmatch(candidate_revision):
                revision = candidate_revision
            worktree_clean = status.stdout == ""

    if repository_readable:
        for name, relative_path in REQUIRED_SOURCE_ARTIFACTS.items():
            try:
                tracked_result = _run_git(
                    root,
                    "ls-files",
                    "--error-unmatch",
                    "--",
                    relative_path,
                )
            except (OSError, subprocess.SubprocessError):
                repository_readable = False
                break
            if tracked_result.returncode != 0:
                continue
            tracked.add(name)
            try:
                artifact_hashes[name] = _sha256_file(
                    root / relative_path,
                    maximum_bytes=_MAX_SOURCE_ARTIFACT_BYTES,
                )
            except (OSError, ReleaseCandidateError):
                artifact_hashes[name] = None

    return RepositorySnapshot(
        release_revision=revision,
        repository_readable=repository_readable,
        worktree_clean=worktree_clean,
        source_artifact_sha256=artifact_hashes,
        tracked_source_artifacts=frozenset(tracked),
    )


def _valid_mysql_evidence(
    payload: Any,
    *,
    expected_scope: str,
    environment_tier: str,
) -> bool:
    return bool(
        isinstance(payload, dict)
        and payload.get("schema_version") == 1
        and payload.get("scope") == expected_scope
        and payload.get("environment") == environment_tier
        and payload.get("status") == "passed"
        and payload.get("counts_as_staging_day") is False
    )


def collect_verification_snapshot(
    evidence_paths: Mapping[str, Path],
    *,
    environment_tier: str,
) -> VerificationSnapshot:
    """Hash required CI outputs and minimally validate the two MySQL contracts."""

    if frozenset(evidence_paths) != frozenset(REQUIRED_VERIFICATION_EVIDENCE):
        raise ReleaseCandidateError("every required verification evidence name is required exactly once")
    resolved = tuple(path.resolve() for path in evidence_paths.values())
    if len(resolved) != len(set(resolved)):
        raise ReleaseCandidateError("verification evidence paths must be distinct")

    digests: dict[str, str | None] = {}
    valid: set[str] = set()
    for name in REQUIRED_VERIFICATION_EVIDENCE:
        path = evidence_paths[name].resolve()
        try:
            digest = _sha256_file(path, maximum_bytes=_MAX_VERIFICATION_EVIDENCE_BYTES)
        except (OSError, ReleaseCandidateError):
            digests[name] = None
            continue
        digests[name] = digest
        if name == "mysql_acceptance":
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not _valid_mysql_evidence(
                payload,
                expected_scope="real_mysql_acceptance",
                environment_tier=environment_tier,
            ):
                continue
        elif name == "mysql_migration":
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not _valid_mysql_evidence(
                payload,
                expected_scope="real_mysql_migration_rehearsal",
                environment_tier=environment_tier,
            ):
                continue
        valid.add(name)
    return VerificationSnapshot(evidence_sha256=digests, valid_evidence=frozenset(valid))


def evaluate_release_candidate(
    *,
    environment_tier: str,
    expected_release_revision: str,
    repository: RepositorySnapshot,
    verification: VerificationSnapshot,
    generated_at: datetime,
    runtime_schema_revision: str = EXPECTED_MARKET_MORNING_SCHEMA_REVISION,
) -> ReleaseCandidateManifest:
    """Bind a clean source tree and verification evidence to one revision."""

    if environment_tier not in {"ci", "staging"}:
        raise ReleaseCandidateError("environment_tier must be ci or staging")
    if not _RELEASE_PATTERN.fullmatch(expected_release_revision):
        raise ReleaseCandidateError("expected_release_revision must be a lowercase immutable revision")
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ReleaseCandidateError("generated_at must be timezone-aware")

    required_artifacts = frozenset(REQUIRED_SOURCE_ARTIFACTS)
    source_hashes_complete = frozenset(repository.source_artifact_sha256) == required_artifacts and all(
        isinstance(value, str) and _SHA256_PATTERN.fullmatch(value)
        for value in repository.source_artifact_sha256.values()
    )
    artifacts_tracked = repository.tracked_source_artifacts == required_artifacts
    required_evidence = frozenset(REQUIRED_VERIFICATION_EVIDENCE)
    evidence_hashes_complete = frozenset(verification.evidence_sha256) == required_evidence and all(
        isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) for value in verification.evidence_sha256.values()
    )
    evidence_valid = verification.valid_evidence == required_evidence
    actual_revision_valid = bool(
        repository.release_revision and _RELEASE_PATTERN.fullmatch(repository.release_revision)
    )
    revision_matches = bool(actual_revision_valid and repository.release_revision == expected_release_revision)
    schema_current = runtime_schema_revision == EXPECTED_MARKET_MORNING_SCHEMA_REVISION

    checks = {
        "artifacts_tracked": artifacts_tracked,
        "clean_worktree": repository.worktree_clean,
        "evidence_hashes_complete": evidence_hashes_complete,
        "evidence_valid": evidence_valid,
        "immutable_revision": actual_revision_valid,
        "release_revision_matches": revision_matches,
        "repository_readable": repository.repository_readable,
        "runtime_schema_current": schema_current,
        "source_hashes_complete": source_hashes_complete,
    }
    blocking_codes = [_BLOCKING_CODE_BY_CHECK[name] for name in sorted(checks) if not checks[name]]
    payload: dict[str, Any] = {
        "schema_version": 1,
        "scope": "market_morning_release_candidate",
        "environment_tier": environment_tier,
        "generated_at": generated_at.isoformat(),
        "expected_release_revision": expected_release_revision,
        "release_revision": repository.release_revision,
        "runtime_schema_revision": runtime_schema_revision,
        "status": "passed" if not blocking_codes else "blocked",
        "counts_as_staging_day": False,
        "counts_as_t1_release_evidence": False,
        "checks": {name: "passed" if value else "failed" for name, value in checks.items()},
        "blocking_codes": blocking_codes,
        "source_artifact_sha256": dict(sorted(repository.source_artifact_sha256.items())),
        "verification_evidence_sha256": dict(sorted(verification.evidence_sha256.items())),
    }
    payload["manifest_sha256"] = _canonical_sha256(payload)
    return ReleaseCandidateManifest(payload=payload)


__all__ = [
    "REQUIRED_SOURCE_ARTIFACTS",
    "REQUIRED_VERIFICATION_EVIDENCE",
    "RELEASE_CANDIDATE_CHECKS",
    "ReleaseCandidateError",
    "ReleaseCandidateManifest",
    "RepositorySnapshot",
    "VerificationSnapshot",
    "collect_repository_snapshot",
    "collect_verification_snapshot",
    "evaluate_release_candidate",
    "parse_release_candidate_manifest",
]
