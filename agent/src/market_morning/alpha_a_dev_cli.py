"""Fail-closed local launcher for the Market Morning Alpha A staging slice.

This command prevents a developer browser session from accidentally using the
product database while exercising real Auth0 flows. It reads the repository
``agent/.env`` with python-dotenv, validates an explicitly separate staging
target, and maps that target to the runtime database variable. For the local
Alpha A slice only, missing public Auth0 runtime values may be filled from the
existing ``frontend/.env`` values without writing either file or exposing
their contents.

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
from types import MappingProxyType
from typing import Any

from dotenv import dotenv_values
from sqlalchemy.engine import make_url

PRODUCT_DATABASE_KEY = "VIBE_MARKET_MORNING_DATABASE_URL"
STAGING_DATABASE_KEY = "VIBE_MARKET_MORNING_STAGING_DATABASE_URL"
FRONTEND_PUBLIC_AUTH_ENV_MAP: Mapping[str, str] = MappingProxyType(
    {
        "VITE_MARKET_MORNING_AUTH_PROVIDER": (
            "VIBE_MARKET_MORNING_PUBLIC_AUTH_PROVIDER"
        ),
        "VITE_MARKET_MORNING_AUTH0_DOMAIN": (
            "VIBE_MARKET_MORNING_PUBLIC_AUTH0_DOMAIN"
        ),
        "VITE_MARKET_MORNING_AUTH0_AUDIENCE": (
            "VIBE_MARKET_MORNING_PUBLIC_AUTH0_AUDIENCE"
        ),
        "VITE_MARKET_MORNING_AUTH0_PRODUCT_CLIENT_ID": (
            "VIBE_MARKET_MORNING_PUBLIC_AUTH0_PRODUCT_CLIENT_ID"
        ),
        "VITE_MARKET_MORNING_AUTH0_OPERATOR_CLIENT_ID": (
            "VIBE_MARKET_MORNING_PUBLIC_AUTH0_OPERATOR_CLIENT_ID"
        ),
    }
)
PUBLIC_AUTH_ENV_KEYS = tuple(FRONTEND_PUBLIC_AUTH_ENV_MAP.values())
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
    frontend_dotenv_mapping: Mapping[str, Any] | None = None,
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
    frontend_values = frontend_dotenv_mapping or {}
    for frontend_key, backend_key in FRONTEND_PUBLIC_AUTH_ENV_MAP.items():
        configured = result.get(backend_key)
        if isinstance(configured, str) and configured.strip():
            result[backend_key] = configured.strip()
            continue
        fallback = frontend_values.get(frontend_key)
        if isinstance(fallback, str) and fallback.strip():
            result[backend_key] = fallback.strip()
    if any(
        not isinstance(result.get(key), str) or not result[key].strip()
        for key in PUBLIC_AUTH_ENV_KEYS
    ):
        raise AlphaADevConfigurationError("alpha_a_public_auth_config_missing")
    result[PRODUCT_DATABASE_KEY] = staging_url
    result["VIBE_MARKET_MORNING_ENABLED"] = "true"
    result["VIBE_MARKET_MORNING_RUNTIME_ENABLED"] = "false"
    return result


def _load_alpha_a_environment(env_path: Path) -> dict[str, str]:
    try:
        values = dotenv_values(env_path)
        frontend_env_path = env_path.parent.parent / "frontend" / ".env"
        frontend_values = (
            dotenv_values(frontend_env_path) if frontend_env_path.is_file() else {}
        )
    except Exception as error:
        raise AlphaADevConfigurationError("alpha_a_env_file_invalid") from error
    return build_alpha_a_environment(
        dotenv_mapping=values,
        frontend_dotenv_mapping=frontend_values,
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
    "FRONTEND_PUBLIC_AUTH_ENV_MAP",
    "PRODUCT_DATABASE_KEY",
    "PUBLIC_AUTH_ENV_KEYS",
    "STAGING_DATABASE_KEY",
    "build_alpha_a_environment",
    "main",
]
