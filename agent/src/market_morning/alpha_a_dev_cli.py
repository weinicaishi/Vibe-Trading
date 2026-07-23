"""Fail-closed local launcher for the Market Morning Alpha A staging slice.

This command prevents a developer browser session from accidentally using the
product database while exercising real Auth0 flows. It reads the repository
``agent/.env`` with python-dotenv, validates an explicitly separate staging
target, and maps that target to the runtime database variable.

The Alpha A slice is local-only and keeps the durable scheduler runtime
disabled. It does not count as a production deployment or as one of the five
required staging trading days.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from sqlalchemy.engine import make_url

PRODUCT_DATABASE_KEY = "VIBE_MARKET_MORNING_DATABASE_URL"
STAGING_DATABASE_KEY = "VIBE_MARKET_MORNING_STAGING_DATABASE_URL"
_LOOPBACK_HOST = "127.0.0.1"
_DEFAULT_PORT = 8898


class AlphaADevConfigurationError(RuntimeError):
    """Safe configuration failure identified only by a stable error code."""


def _configured_value(values: Mapping[str, Any], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AlphaADevConfigurationError(f"{key.lower()}_missing")
    return value.strip()


def _database_target(
    database_url: str,
    *,
    require_staging_name: bool,
) -> tuple[str, str, int, str]:
    try:
        parsed = make_url(database_url)
    except Exception as error:
        raise AlphaADevConfigurationError("alpha_a_database_url_invalid") from error
    driver = parsed.drivername.split("+", 1)[0].lower()
    host = (parsed.host or "").strip().lower()
    database = (parsed.database or "").strip()
    port = parsed.port or 3306
    if driver != "mysql" or not host or not database or not (1 <= port <= 65535):
        raise AlphaADevConfigurationError("alpha_a_database_url_invalid")
    if require_staging_name and "staging" not in database.casefold():
        raise AlphaADevConfigurationError("alpha_a_staging_database_name_invalid")
    return driver, host, port, database.casefold()


def build_alpha_a_environment(
    *,
    dotenv_mapping: Mapping[str, Any],
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a child environment that can only target the Alpha A staging DB."""

    product_url = _configured_value(dotenv_mapping, PRODUCT_DATABASE_KEY)
    staging_url = _configured_value(dotenv_mapping, STAGING_DATABASE_KEY)
    product_target = _database_target(product_url, require_staging_name=False)
    staging_target = _database_target(staging_url, require_staging_name=True)
    if product_target == staging_target:
        raise AlphaADevConfigurationError("alpha_a_staging_database_matches_product")

    result = dict(base_environment or {})
    result.update(
        {
            key: value
            for key, value in dotenv_mapping.items()
            if isinstance(key, str) and isinstance(value, str)
        }
    )
    result[PRODUCT_DATABASE_KEY] = staging_url
    result["VIBE_MARKET_MORNING_ENABLED"] = "true"
    result["VIBE_MARKET_MORNING_RUNTIME_ENABLED"] = "false"
    return result


def _load_alpha_a_environment(env_path: Path) -> dict[str, str]:
    try:
        values = dotenv_values(env_path)
    except Exception as error:
        raise AlphaADevConfigurationError("alpha_a_env_file_invalid") from error
    return build_alpha_a_environment(
        dotenv_mapping=values,
        base_environment=os.environ,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the local Market Morning Alpha A slice against its dedicated "
            "staging database."
        )
    )
    parser.add_argument("--host", default=_LOOPBACK_HOST)
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Validate the isolated staging target without starting a server.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.host != _LOOPBACK_HOST:
        print("alpha_a_loopback_host_required", file=sys.stderr)
        return 2
    if not 1 <= args.port <= 65535:
        print("alpha_a_port_invalid", file=sys.stderr)
        return 2

    agent_dir = Path(__file__).resolve().parents[2]
    try:
        child_environment = _load_alpha_a_environment(agent_dir / ".env")
    except AlphaADevConfigurationError as error:
        print(str(error), file=sys.stderr)
        return 2

    if args.check_only:
        print(
            json.dumps(
                {
                    "counts_as_staging_day": False,
                    "host": args.host,
                    "port": args.port,
                    "runtime_enabled": False,
                    "scope": "market_morning_alpha_a_local_staging",
                    "status": "passed",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0

    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "api_server:app",
        "--app-dir",
        str(agent_dir),
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    os.execvpe(sys.executable, command, child_environment)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "AlphaADevConfigurationError",
    "PRODUCT_DATABASE_KEY",
    "STAGING_DATABASE_KEY",
    "build_alpha_a_environment",
    "main",
]
