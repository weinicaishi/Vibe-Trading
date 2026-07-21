"""Fail-closed contracts for a controlled Market Morning schema upgrade."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any

from sqlalchemy.engine import URL, make_url

from src.market_morning.db import EXPECTED_MARKET_MORNING_SCHEMA_REVISION


_ACCEPTANCE_DATABASE_PATTERN = re.compile(
    r"^market_morning_(?:migration_)?acceptance(?:_[a-z0-9]+)*$"
)
_REVISION_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}$")


class ProductionSchemaChangeTargetError(ValueError):
    """Raised before a production schema command can touch an unsafe target."""


@dataclass(frozen=True, slots=True)
class ProductionDatabaseTarget:
    database: str
    target_fingerprint: str


@dataclass(frozen=True, slots=True)
class SchemaSnapshot:
    revision: str | None
    market_morning_table_count: int

    def __post_init__(self) -> None:
        if self.revision is not None and not _REVISION_PATTERN.fullmatch(self.revision):
            raise ValueError("schema snapshot revision is invalid")
        if self.market_morning_table_count < 0:
            raise ValueError("schema snapshot table count cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "market_morning_table_count": self.market_morning_table_count,
        }


@dataclass(frozen=True, slots=True)
class SchemaChangeExecution:
    exit_code: int
    duration_ms: int
    output_sha256: str

    def __post_init__(self) -> None:
        if self.duration_ms < 0:
            raise ValueError("schema change duration cannot be negative")
        if not _SHA256_PATTERN.fullmatch(self.output_sha256):
            raise ValueError("schema change output hash must be lowercase SHA-256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "output_sha256": self.output_sha256,
        }


@dataclass(frozen=True, slots=True)
class ProductionSchemaChangeManifest:
    run_id: str
    environment: str
    target_fingerprint: str
    expected_source_revision: str
    target_revision: str
    backup_evidence_sha256: str | None
    change_reference_sha256: str | None
    started_at: str
    finished_at: str
    check_only: bool
    status: str
    failure_code: str | None
    before: SchemaSnapshot | None
    after: SchemaSnapshot | None
    execution: SchemaChangeExecution | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "scope": "production_schema_change",
            "environment": self.environment,
            "target_fingerprint": self.target_fingerprint,
            "expected_source_revision": self.expected_source_revision,
            "target_revision": self.target_revision,
            "backup_evidence_sha256": self.backup_evidence_sha256,
            "change_reference_sha256": self.change_reference_sha256,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "check_only": self.check_only,
            "status": self.status,
            "failure_code": self.failure_code,
            "before": None if self.before is None else self.before.to_dict(),
            "after": None if self.after is None else self.after.to_dict(),
            "execution": None if self.execution is None else self.execution.to_dict(),
            "automatic_downgrade_attempted": False,
            "contains_backup": False,
            "contains_credentials": False,
            "counts_as_staging_day": False,
        }


def _parse_mysql_url(raw_url: str, *, label: str) -> URL:
    if not raw_url.strip():
        raise ProductionSchemaChangeTargetError(f"{label} database URL is required")
    try:
        parsed = make_url(raw_url)
    except Exception as error:
        raise ProductionSchemaChangeTargetError(f"{label} database URL is invalid") from error
    if parsed.drivername != "mysql+asyncmy":
        raise ProductionSchemaChangeTargetError(
            f"{label} database URL must use mysql+asyncmy"
        )
    if not parsed.host or not parsed.database:
        raise ProductionSchemaChangeTargetError(
            f"{label} database URL must name a host and database"
        )
    return parsed


def _target_identity(parsed: URL) -> tuple[str, int, str]:
    assert parsed.host is not None
    assert parsed.database is not None
    return parsed.host.lower(), parsed.port or 3306, parsed.database.lower()


def validate_production_database_url(
    raw_url: str,
    *,
    acceptance_url: str = "",
    migration_acceptance_url: str = "",
) -> ProductionDatabaseTarget:
    """Reject disposable or aliased test targets before any schema write."""

    parsed = _parse_mysql_url(raw_url, label="production")
    identity = _target_identity(parsed)
    database = identity[2]
    if _ACCEPTANCE_DATABASE_PATTERN.fullmatch(database):
        raise ProductionSchemaChangeTargetError(
            "production URL must not name an acceptance database"
        )
    for label, candidate in (
        ("acceptance", acceptance_url),
        ("migration acceptance", migration_acceptance_url),
    ):
        if not candidate.strip():
            continue
        candidate_identity = _target_identity(_parse_mysql_url(candidate, label=label))
        if identity == candidate_identity:
            raise ProductionSchemaChangeTargetError(
                "production target must differ from every acceptance database"
            )
    fingerprint_input = f"{identity[0]}:{identity[1]}/{identity[2]}"
    return ProductionDatabaseTarget(
        database=database,
        target_fingerprint=hashlib.sha256(
            fingerprint_input.encode("utf-8")
        ).hexdigest(),
    )


def validate_revision(value: str, *, label: str) -> str:
    canonical = value.strip()
    if not _REVISION_PATTERN.fullmatch(canonical):
        raise ValueError(f"{label} must be a 1 to 64 character revision identifier")
    return canonical


def validate_sha256(value: str, *, label: str) -> str:
    canonical = value.strip().lower()
    if not _SHA256_PATTERN.fullmatch(canonical):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return canonical


def hash_change_reference(value: str) -> str:
    canonical = value.strip()
    if not _REFERENCE_PATTERN.fullmatch(canonical):
        raise ValueError(
            "change reference must contain 3 to 128 safe identifier characters"
        )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def expected_target_revision() -> str:
    return EXPECTED_MARKET_MORNING_SCHEMA_REVISION


__all__ = [
    "ProductionDatabaseTarget",
    "ProductionSchemaChangeManifest",
    "ProductionSchemaChangeTargetError",
    "SchemaChangeExecution",
    "SchemaSnapshot",
    "expected_target_revision",
    "hash_change_reference",
    "validate_production_database_url",
    "validate_revision",
    "validate_sha256",
]
