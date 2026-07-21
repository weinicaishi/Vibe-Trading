"""Docker Compose process and safety contract for Market Morning."""

from __future__ import annotations

from pathlib import Path

import yaml


COMPOSE_PATH = Path("docker-compose.yml")


def _compose() -> dict:
    payload = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_market_morning_runtime_is_opt_in_and_separate_from_api() -> None:
    services = _compose()["services"]
    runtime = services["market-morning-runtime"]
    api = services["vibe-trading"]

    assert runtime["profiles"] == ["market-morning"]
    assert runtime["build"] == "."
    assert runtime["command"] == [
        "/opt/venv/bin/vibe-trading-market-morning"
    ]
    assert runtime["env_file"] == ["agent/.env"]
    assert runtime["healthcheck"] == {"disable": True}
    assert "environment" not in runtime
    assert "ports" not in runtime
    assert runtime["command"] != api.get("command")


def test_market_morning_runtime_is_fail_closed_and_hardened() -> None:
    runtime = _compose()["services"]["market-morning-runtime"]

    assert runtime["init"] is True
    assert runtime["read_only"] is True
    assert runtime["cap_drop"] == ["ALL"]
    assert runtime["security_opt"] == ["no-new-privileges:true"]
    assert runtime["restart"] == "on-failure:5"
    assert runtime["stop_grace_period"] == "30s"
    assert runtime["pids_limit"] == 256
    assert set(runtime["tmpfs"]) == {
        "/tmp",
        "/home/vibe/.cache",
        "/home/vibe/.config",
    }


def test_market_morning_runtime_never_runs_migrations_or_exposes_ports() -> None:
    runtime = _compose()["services"]["market-morning-runtime"]
    encoded_command = " ".join(runtime["command"]).lower()

    assert "alembic" not in encoded_command
    assert "migration" not in encoded_command
    assert "production-schema-change" not in encoded_command
    assert "ports" not in runtime
    assert "expose" not in runtime
