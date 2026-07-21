"""Controlled, hash-only Market Morning production schema change command."""

from __future__ import annotations

import argparse
import asyncio
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

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.market_morning.models import Base
from src.market_morning.production_schema_change import (
    ProductionSchemaChangeManifest,
    ProductionSchemaChangeTargetError,
    SchemaChangeExecution,
    SchemaSnapshot,
    expected_target_revision,
    hash_change_reference,
    validate_production_database_url,
    validate_revision,
    validate_sha256,
)


EXPECTED_HEAD_TABLE_COUNT = len(
    [name for name in Base.metadata.tables if name.startswith("mm_")]
)


class SchemaInspector(Protocol):
    def __call__(self, database_url: str) -> SchemaSnapshot: ...


class SchemaChangeExecutor(Protocol):
    def __call__(self, child_environment: Mapping[str, str]) -> SchemaChangeExecution: ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def _inspect_async(database_url: str) -> SchemaSnapshot:
    engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 10},
    )
    try:
        async with engine.connect() as connection:
            revision = (
                await connection.execute(
                    text("SELECT version_num FROM alembic_version LIMIT 1")
                )
            ).scalar_one_or_none()
            table_count = (
                await connection.execute(
                    text(
                        "SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_schema = DATABASE() AND LEFT(table_name, 3) = 'mm_'"
                    )
                )
            ).scalar_one()
        return SchemaSnapshot(
            revision=None if revision is None else str(revision),
            market_morning_table_count=int(table_count),
        )
    finally:
        await engine.dispose()


def _default_inspector(database_url: str) -> SchemaSnapshot:
    return asyncio.run(_inspect_async(database_url))


def _default_executor(
    child_environment: Mapping[str, str],
) -> SchemaChangeExecution:
    started = time.monotonic()
    completed = subprocess.run(  # noqa: S603 - fixed executable and arguments
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "agent/alembic-market-morning.ini",
            "upgrade",
            "head",
        ],
        check=False,
        cwd=Path.cwd(),
        env=dict(child_environment),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return SchemaChangeExecution(
        exit_code=completed.returncode,
        duration_ms=round((time.monotonic() - started) * 1_000),
        output_sha256=hashlib.sha256(completed.stdout).hexdigest(),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Check or upgrade the configured Market Morning product schema with "
            "fail-closed revision and maintenance controls."
        )
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--environment", required=True, choices=("staging", "production"))
    parser.add_argument("--expected-current-revision", required=True)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--backup-evidence-sha256", default="")
    parser.add_argument("--change-reference", default="")
    parser.add_argument("--confirm-maintenance-window", action="store_true")
    parser.add_argument("--confirm-runtime-stopped", action="store_true")
    parser.add_argument("--confirm-no-automatic-downgrade", action="store_true")
    parser.add_argument("--confirm-production-schema-change", action="store_true")
    return parser


def _write_manifest(destination: Path, payload: dict) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def _runtime_enabled(environ: Mapping[str, str]) -> bool:
    return environ.get("VIBE_MARKET_MORNING_RUNTIME_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _failed_manifest(
    manifest: ProductionSchemaChangeManifest,
    *,
    failure_code: str,
    before: SchemaSnapshot | None = None,
    after: SchemaSnapshot | None = None,
    execution: SchemaChangeExecution | None = None,
) -> ProductionSchemaChangeManifest:
    return replace(
        manifest,
        finished_at=_utc_now(),
        status="failed",
        failure_code=failure_code,
        before=before,
        after=after,
        execution=execution,
    )


def run_cli(
    argv: list[str] | None = None,
    *,
    inspector: SchemaInspector | None = None,
    executor: SchemaChangeExecutor | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    parent_environment = dict(os.environ if environ is None else environ)
    raw_url = parent_environment.get("VIBE_MARKET_MORNING_DATABASE_URL", "")
    try:
        target = validate_production_database_url(
            raw_url,
            acceptance_url=parent_environment.get(
                "VIBE_MARKET_MORNING_ACCEPTANCE_DATABASE_URL", ""
            ),
            migration_acceptance_url=parent_environment.get(
                "VIBE_MARKET_MORNING_MIGRATION_DATABASE_URL", ""
            ),
        )
        expected_source = validate_revision(
            args.expected_current_revision,
            label="expected current revision",
        )
        backup_hash = (
            None
            if args.check_only
            else validate_sha256(
                args.backup_evidence_sha256,
                label="backup evidence",
            )
        )
        change_reference_hash = (
            None
            if args.check_only
            else hash_change_reference(args.change_reference)
        )
    except (ProductionSchemaChangeTargetError, ValueError) as error:
        parser.error(str(error))

    if not args.check_only:
        confirmations = (
            args.confirm_maintenance_window,
            args.confirm_runtime_stopped,
            args.confirm_no_automatic_downgrade,
            args.confirm_production_schema_change,
        )
        if not all(confirmations):
            parser.error("all production schema change confirmations are required")
        if _runtime_enabled(parent_environment):
            parser.error("Market Morning runtime must be disabled before schema change")

    started_at = _utc_now()
    manifest = ProductionSchemaChangeManifest(
        run_id=f"production-schema-change-{uuid4()}",
        environment=args.environment,
        target_fingerprint=target.target_fingerprint,
        expected_source_revision=expected_source,
        target_revision=expected_target_revision(),
        backup_evidence_sha256=backup_hash,
        change_reference_sha256=change_reference_hash,
        started_at=started_at,
        finished_at=started_at,
        check_only=args.check_only,
        status="pending",
        failure_code=None,
        before=None,
        after=None,
        execution=None,
    )
    inspect_schema = inspector or _default_inspector
    try:
        before = inspect_schema(raw_url)
    except Exception:
        failed = _failed_manifest(manifest, failure_code="preflight_unavailable")
        _write_manifest(args.output, failed.to_dict())
        return 1
    if before.revision != expected_source:
        failed = _failed_manifest(
            manifest,
            failure_code="source_revision_mismatch",
            before=before,
        )
        _write_manifest(args.output, failed.to_dict())
        return 1
    if (
        before.revision == expected_target_revision()
        and before.market_morning_table_count != EXPECTED_HEAD_TABLE_COUNT
    ):
        failed = _failed_manifest(
            manifest,
            failure_code="source_schema_incomplete",
            before=before,
        )
        _write_manifest(args.output, failed.to_dict())
        return 1
    if args.check_only:
        checked = replace(
            manifest,
            finished_at=_utc_now(),
            status="passed",
            before=before,
            after=before,
        )
        _write_manifest(args.output, checked.to_dict())
        return 0
    if before.revision == expected_target_revision():
        current = replace(
            manifest,
            finished_at=_utc_now(),
            status="passed",
            before=before,
            after=before,
        )
        _write_manifest(args.output, current.to_dict())
        return 0

    child_environment = dict(parent_environment)
    child_environment.update(
        {
            "VIBE_MARKET_MORNING_DATABASE_URL": raw_url,
            "VIBE_MARKET_MORNING_RUNTIME_ENABLED": "false",
            "VIBE_MARKET_MORNING_PRODUCTION_SCHEMA_CHANGE_CONFIRMED": "true",
        }
    )
    run_upgrade = executor or _default_executor
    try:
        execution = run_upgrade(child_environment)
    except Exception:
        failed = _failed_manifest(
            manifest,
            failure_code="migration_execution_error",
            before=before,
        )
        _write_manifest(args.output, failed.to_dict())
        return 1
    if execution.exit_code != 0:
        failed = _failed_manifest(
            manifest,
            failure_code="migration_nonzero_exit",
            before=before,
            execution=execution,
        )
        _write_manifest(args.output, failed.to_dict())
        return 1
    try:
        after = inspect_schema(raw_url)
    except Exception:
        failed = _failed_manifest(
            manifest,
            failure_code="postflight_unavailable",
            before=before,
            execution=execution,
        )
        _write_manifest(args.output, failed.to_dict())
        return 1
    if (
        after.revision != expected_target_revision()
        or after.market_morning_table_count != EXPECTED_HEAD_TABLE_COUNT
    ):
        failed = _failed_manifest(
            manifest,
            failure_code="postflight_schema_mismatch",
            before=before,
            after=after,
            execution=execution,
        )
        _write_manifest(args.output, failed.to_dict())
        return 1
    passed = replace(
        manifest,
        finished_at=_utc_now(),
        status="passed",
        before=before,
        after=after,
        execution=execution,
    )
    _write_manifest(args.output, passed.to_dict())
    return 0


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
