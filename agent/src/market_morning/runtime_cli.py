"""Console entrypoint for the isolated Market Morning process runtime."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from dataclasses import replace
from datetime import timedelta

from src.config.accessor import get_env_config
from src.market_morning.db import get_session_factory, reset_database_state
from src.market_morning.runtime import (
    MarketMorningRuntimeConfigurationError,
    MarketMorningRuntimeRole,
    install_shutdown_signal_handlers,
    load_runtime_dependencies,
    run_market_morning_runtime,
)

logger = logging.getLogger(__name__)


def _instance_id(configured: str, *, role: str) -> str:
    if configured.strip():
        return configured.strip()
    host = socket.gethostname().strip() or "unknown-host"
    return f"market-morning-{role}-{host}-{os.getpid()}"[:128]


async def _run() -> None:
    config = get_env_config().market_morning
    if not config.enabled:
        raise MarketMorningRuntimeConfigurationError("market_morning_disabled")
    if not config.runtime_enabled:
        raise MarketMorningRuntimeConfigurationError("runtime_disabled")

    dependencies = await load_runtime_dependencies(config.runtime_factory)
    if dependencies.session_factory is None:
        dependencies = replace(
            dependencies,
            session_factory=get_session_factory(),
        )
    role = MarketMorningRuntimeRole(config.runtime_role)
    stop_event = asyncio.Event()
    install_shutdown_signal_handlers(stop_event)
    summary = await run_market_morning_runtime(
        role=role,
        dependencies=dependencies,
        stop_event=stop_event,
        worker_id=_instance_id(config.worker_id, role="worker"),
        scheduler_owner_id=_instance_id(
            config.scheduler_owner_id,
            role="scheduler",
        ),
        allow_fixture_dependencies=config.allow_fixture_runtime,
        worker_poll_interval=timedelta(seconds=config.worker_poll_seconds),
        scheduler_poll_interval=timedelta(
            seconds=config.scheduler_poll_seconds
        ),
    )
    logger.info(
        "Market Morning runtime stopped: role=%s worker_iterations=%s scheduler_iterations=%s",
        summary.role.value,
        summary.worker.iterations if summary.worker is not None else 0,
        summary.scheduler.iterations if summary.scheduler is not None else 0,
    )


def main() -> None:
    """Run the configured process without printing secrets or provider errors."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    exit_code = 0
    try:
        asyncio.run(_run())
    except MarketMorningRuntimeConfigurationError as error:
        logger.error(
            "Market Morning runtime startup rejected: error_code=%s",
            error.error_code,
        )
        exit_code = 2
    except KeyboardInterrupt:
        exit_code = 130
    except Exception as error:
        logger.error(
            "Market Morning runtime stopped unexpectedly: exception_type=%s",
            type(error).__name__,
        )
        exit_code = 1
    finally:
        try:
            asyncio.run(reset_database_state())
        except Exception as error:
            logger.error(
                "Market Morning database shutdown failed: exception_type=%s",
                type(error).__name__,
            )
            exit_code = exit_code or 1
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
