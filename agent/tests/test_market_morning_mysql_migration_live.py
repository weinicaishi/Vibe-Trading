"""Destructive migration probes for an explicitly disposable MySQL database."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import text

from src.market_morning.db import (
    EXPECTED_MARKET_MORNING_SCHEMA_REVISION,
    get_engine,
    reset_database_state,
)
from src.market_morning.models import Base
from src.market_morning.mysql_migration_rehearsal import (
    validate_migration_database_url,
)


pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
REVISION_0016 = "0016_market_morning_model_usage"


def _require_confirmed_migration_target() -> None:
    if os.environ.get("VIBE_MARKET_MORNING_MYSQL_MIGRATION_CONFIRMED") != "true":
        pytest.skip("destructive MySQL migration confirmation is not enabled")
    migration_url = os.environ.get(
        "VIBE_MARKET_MORNING_MIGRATION_DATABASE_URL",
        "",
    )
    product_url = os.environ.get("VIBE_MARKET_MORNING_DATABASE_URL", "")
    migration_target = validate_migration_database_url(migration_url)
    product_target = validate_migration_database_url(product_url)
    if migration_target.target_fingerprint != product_target.target_fingerprint:
        pytest.fail("product database URL does not match the confirmed migration target")


def _alembic_config() -> Config:
    return Config(str(REPO_ROOT / "agent/alembic-market-morning.ini"))


def _migrate(direction: str, revision: str) -> None:
    asyncio.run(reset_database_state())
    if direction == "upgrade":
        command.upgrade(_alembic_config(), revision)
    elif direction == "downgrade":
        command.downgrade(_alembic_config(), revision)
    else:
        raise ValueError("unsupported migration direction")
    asyncio.run(reset_database_state())


async def _schema_state() -> tuple[str | None, frozenset[str]]:
    await reset_database_state()
    try:
        engine = get_engine()
        async with engine.connect() as connection:
            revision = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
            tables = (
                (
                    await connection.execute(
                        text(
                            "SELECT TABLE_NAME FROM information_schema.TABLES "
                            "WHERE TABLE_SCHEMA = DATABASE() "
                            "AND TABLE_NAME LIKE 'mm\\_%' "
                            "ORDER BY TABLE_NAME"
                        )
                    )
                )
                .scalars()
                .all()
            )
            return (
                None if revision is None else str(revision),
                frozenset(str(name) for name in tables),
            )
    finally:
        await reset_database_state()


async def _insert_0015_seed(*, user_id: str, invite_id: str, token_hash: str) -> None:
    await reset_database_state()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        engine = get_engine()
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO mm_users "
                    "(user_id, external_subject, timezone, email_opt_in, "
                    "account_status, trial_or_subscription_status, "
                    "last_product_activity_at, created_at, updated_at, deleted_at) "
                    "VALUES (:user_id, :external_subject, 'Asia/Tokyo', 0, "
                    "'active', 'private_beta', NULL, :now, :now, NULL)"
                ),
                {
                    "user_id": user_id,
                    "external_subject": f"mysql-migration:{user_id}",
                    "now": now,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO mm_private_beta_invites "
                    "(invite_id, token_sha256, status, expires_at, "
                    "created_by_reference, accepted_by_user_id, accepted_at, "
                    "revoked_at, created_at, updated_at) "
                    "VALUES (:invite_id, :token_hash, 'pending', :expires_at, "
                    "'mysql-migration-rehearsal', NULL, NULL, NULL, :now, :now)"
                ),
                {
                    "invite_id": invite_id,
                    "token_hash": token_hash,
                    "expires_at": now,
                    "now": now,
                },
            )
    finally:
        await reset_database_state()


async def _seed_counts(*, user_id: str, invite_id: str) -> tuple[int, int]:
    await reset_database_state()
    try:
        engine = get_engine()
        async with engine.connect() as connection:
            user_count = int(
                (
                    await connection.execute(
                        text("SELECT COUNT(*) FROM mm_users WHERE user_id = :user_id"),
                        {"user_id": user_id},
                    )
                ).scalar_one()
            )
            invite_count = int(
                (
                    await connection.execute(
                        text("SELECT COUNT(*) FROM mm_private_beta_invites WHERE invite_id = :invite_id"),
                        {"invite_id": invite_id},
                    )
                ).scalar_one()
            )
            return user_count, invite_count
    finally:
        await reset_database_state()


async def _delete_seed(*, user_id: str, invite_id: str) -> None:
    await reset_database_state()
    try:
        engine = get_engine()
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM mm_private_beta_invites WHERE invite_id = :invite_id"),
                {"invite_id": invite_id},
            )
            await connection.execute(
                text("DELETE FROM mm_users WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
    finally:
        await reset_database_state()


def _restore_head_and_delete_seed(
    *,
    user_id: str | None = None,
    invite_id: str | None = None,
) -> None:
    _migrate("upgrade", "head")
    if user_id is not None and invite_id is not None:
        asyncio.run(_delete_seed(user_id=user_id, invite_id=invite_id))


def test_mysql_migration_fresh_database_reaches_exact_head() -> None:
    _require_confirmed_migration_target()

    try:
        _migrate("downgrade", "base")
        base_revision, base_tables = asyncio.run(_schema_state())
        assert base_revision is None
        assert base_tables == frozenset()

        _migrate("upgrade", "head")
        head_revision, head_tables = asyncio.run(_schema_state())
        assert head_revision == EXPECTED_MARKET_MORNING_SCHEMA_REVISION
        assert head_tables == frozenset(Base.metadata.tables)
    finally:
        _restore_head_and_delete_seed()


def test_mysql_migration_0016_data_survives_upgrade_to_head() -> None:
    _require_confirmed_migration_target()
    user_id = str(uuid4())
    invite_id = str(uuid4())

    try:
        _migrate("downgrade", "base")
        _migrate("upgrade", REVISION_0016)
        revision, tables = asyncio.run(_schema_state())
        assert revision == REVISION_0016
        assert "mm_model_usage_events" in tables
        assert "mm_content_reports" not in tables

        asyncio.run(
            _insert_0015_seed(
                user_id=user_id,
                invite_id=invite_id,
                token_hash="a" * 64,
            )
        )
        _migrate("upgrade", "head")

        head_revision, head_tables = asyncio.run(_schema_state())
        counts = asyncio.run(_seed_counts(user_id=user_id, invite_id=invite_id))
        assert head_revision == EXPECTED_MARKET_MORNING_SCHEMA_REVISION
        assert "mm_model_usage_events" in head_tables
        assert "mm_content_reports" in head_tables
        assert "mm_auth_sessions" in head_tables
        assert counts == (1, 1)
    finally:
        _restore_head_and_delete_seed(user_id=user_id, invite_id=invite_id)


def test_mysql_migration_downgrade_roundtrip_preserves_prior_data() -> None:
    _require_confirmed_migration_target()
    user_id = str(uuid4())
    invite_id = str(uuid4())

    try:
        _migrate("downgrade", "base")
        _migrate("upgrade", "head")
        asyncio.run(
            _insert_0015_seed(
                user_id=user_id,
                invite_id=invite_id,
                token_hash="b" * 64,
            )
        )

        _migrate("downgrade", REVISION_0016)
        downgraded_revision, downgraded_tables = asyncio.run(_schema_state())
        downgraded_counts = asyncio.run(_seed_counts(user_id=user_id, invite_id=invite_id))
        assert downgraded_revision == REVISION_0016
        assert "mm_model_usage_events" in downgraded_tables
        assert "mm_content_reports" not in downgraded_tables
        assert "mm_auth_sessions" not in downgraded_tables
        assert downgraded_counts == (1, 1)

        _migrate("upgrade", "head")
        restored_revision, restored_tables = asyncio.run(_schema_state())
        restored_counts = asyncio.run(_seed_counts(user_id=user_id, invite_id=invite_id))
        assert restored_revision == EXPECTED_MARKET_MORNING_SCHEMA_REVISION
        assert "mm_model_usage_events" in restored_tables
        assert "mm_content_reports" in restored_tables
        assert "mm_auth_sessions" in restored_tables
        assert restored_counts == (1, 1)
    finally:
        _restore_head_and_delete_seed(user_id=user_id, invite_id=invite_id)
