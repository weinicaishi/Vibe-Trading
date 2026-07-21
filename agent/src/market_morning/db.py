"""Lazy MySQL engine and session management for Market Morning."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config.accessor import get_env_config

logger = logging.getLogger(__name__)

EXPECTED_MARKET_MORNING_SCHEMA_REVISION = "0017_market_morning_content_reports"


class MarketMorningDatabaseNotConfigured(RuntimeError):
    """Raised when product code asks for a database without a valid URL."""


_engine: AsyncEngine | None = None
_engine_url: str | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _validated_database_url(raw_url: str) -> URL:
    """Return a parsed async MySQL URL without ever logging credentials."""
    if not raw_url.strip():
        raise MarketMorningDatabaseNotConfigured(
            "VIBE_MARKET_MORNING_DATABASE_URL is required when Market Morning is enabled"
        )
    try:
        url = make_url(raw_url)
    except Exception as exc:  # SQLAlchemy raises several URL parse errors
        raise MarketMorningDatabaseNotConfigured("Market Morning database URL is invalid") from exc
    if url.drivername != "mysql+asyncmy":
        raise MarketMorningDatabaseNotConfigured(
            "Market Morning database URL must use the mysql+asyncmy driver"
        )
    if not url.database:
        raise MarketMorningDatabaseNotConfigured("Market Morning database URL must name a database")
    return url


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, creating it only on first use."""
    global _engine, _engine_url, _session_factory

    cfg = get_env_config().market_morning
    parsed = _validated_database_url(cfg.database_url)
    rendered = parsed.render_as_string(hide_password=False)

    if _engine is not None:
        if _engine_url != rendered:
            raise RuntimeError(
                "Market Morning database URL changed after engine initialization; restart the process"
            )
        return _engine

    engine = create_async_engine(
        parsed,
        echo=cfg.database_echo,
        pool_pre_ping=True,
        pool_size=cfg.database_pool_size,
        pool_recycle=cfg.database_pool_recycle_seconds,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _set_utc_session(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SET time_zone = '+00:00'")
        finally:
            cursor.close()

    _engine = engine
    _engine_url = rendered
    _session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the configured session factory."""
    if _session_factory is None:
        get_engine()
    assert _session_factory is not None
    return _session_factory


async def session_scope() -> AsyncIterator[AsyncSession]:
    """Yield a transactional session and roll back on failure."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def probe_database() -> tuple[bool, str]:
    """Check connectivity and the exact product schema revision."""
    cfg = get_env_config().market_morning
    if not cfg.enabled:
        return False, "disabled"
    try:
        engine = get_engine()
        async with engine.connect() as connection:
            result = await connection.execute(
                text("SELECT version_num FROM alembic_version")
            )
            revision = result.scalar_one_or_none()
    except MarketMorningDatabaseNotConfigured as exc:
        return False, str(exc)
    except Exception as exc:  # readiness must fail closed, not crash the host API
        logger.warning("Market Morning database probe failed: %s", type(exc).__name__)
        return False, "database unavailable"
    if revision != EXPECTED_MARKET_MORNING_SCHEMA_REVISION:
        return False, "Market Morning database schema is not current"
    return True, "ready"


async def reset_database_state() -> None:
    """Dispose cached state; intended for shutdown and isolated tests."""
    global _engine, _engine_url, _session_factory
    engine = _engine
    _engine = None
    _engine_url = None
    _session_factory = None
    if engine is not None:
        await engine.dispose()
