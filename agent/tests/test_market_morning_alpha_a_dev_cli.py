from __future__ import annotations

import json

import pytest

from src.market_morning import alpha_a_dev_cli


def _values(
    *,
    product_database: str = "market_morning_product",
    staging_database: str = "market_morning_alpha_a_staging",
) -> dict[str, str]:
    return {
        alpha_a_dev_cli.PRODUCT_DATABASE_KEY: (
            f"mysql+asyncmy://product:secret@db.example:3306/"
            f"{product_database}?charset=utf8mb4"
        ),
        alpha_a_dev_cli.STAGING_DATABASE_KEY: (
            f"mysql+asyncmy://staging:secret@db.example:3306/"
            f"{staging_database}?charset=utf8mb4"
        ),
        "VIBE_MARKET_MORNING_RUNTIME_ENABLED": "true",
        "VIBE_MARKET_MORNING_PUBLIC_AUTH_PROVIDER": "auth0",
        "VIBE_MARKET_MORNING_PUBLIC_AUTH0_DOMAIN": "tenant.jp.auth0.com",
        "VIBE_MARKET_MORNING_PUBLIC_AUTH0_AUDIENCE": (
            "https://api.market-morning.example"
        ),
        "VIBE_MARKET_MORNING_PUBLIC_AUTH0_PRODUCT_CLIENT_ID": (
            "product-client-id"
        ),
        "VIBE_MARKET_MORNING_PUBLIC_AUTH0_OPERATOR_CLIENT_ID": (
            "operator-client-id"
        ),
    }


def test_build_alpha_a_environment_maps_staging_and_disables_runtime() -> None:
    values = _values()

    result = alpha_a_dev_cli.build_alpha_a_environment(
        dotenv_mapping=values,
        base_environment={"UNCHANGED": "yes"},
    )

    assert result["UNCHANGED"] == "yes"
    assert result[alpha_a_dev_cli.PRODUCT_DATABASE_KEY] == values[
        alpha_a_dev_cli.STAGING_DATABASE_KEY
    ]
    assert result["VIBE_MARKET_MORNING_ENABLED"] == "true"
    assert result["VIBE_MARKET_MORNING_RUNTIME_ENABLED"] == "false"


def test_build_alpha_a_environment_fills_public_auth_from_frontend_without_writing() -> None:
    values = {
        key: value
        for key, value in _values().items()
        if key not in alpha_a_dev_cli.PUBLIC_AUTH_ENV_KEYS
    }
    frontend_values = {
        frontend_key: f"value-{index}"
        for index, frontend_key in enumerate(
            alpha_a_dev_cli.FRONTEND_PUBLIC_AUTH_ENV_MAP,
            start=1,
        )
    }

    result = alpha_a_dev_cli.build_alpha_a_environment(
        dotenv_mapping=values,
        frontend_dotenv_mapping=frontend_values,
    )

    assert {
        backend_key: result[backend_key]
        for backend_key in alpha_a_dev_cli.PUBLIC_AUTH_ENV_KEYS
    } == {
        backend_key: frontend_values[frontend_key]
        for frontend_key, backend_key in (
            alpha_a_dev_cli.FRONTEND_PUBLIC_AUTH_ENV_MAP.items()
        )
    }
    assert not any(key.startswith("VITE_") for key in result)


def test_build_alpha_a_environment_prefers_explicit_backend_public_auth() -> None:
    values = _values()
    frontend_values = {
        frontend_key: "must-not-win"
        for frontend_key in alpha_a_dev_cli.FRONTEND_PUBLIC_AUTH_ENV_MAP
    }

    result = alpha_a_dev_cli.build_alpha_a_environment(
        dotenv_mapping=values,
        frontend_dotenv_mapping=frontend_values,
    )

    assert result["VIBE_MARKET_MORNING_PUBLIC_AUTH0_DOMAIN"] == (
        "tenant.jp.auth0.com"
    )


def test_build_alpha_a_environment_requires_complete_public_auth() -> None:
    values = {
        key: value
        for key, value in _values().items()
        if key not in alpha_a_dev_cli.PUBLIC_AUTH_ENV_KEYS
    }

    with pytest.raises(
        alpha_a_dev_cli.AlphaADevConfigurationError,
        match="^alpha_a_public_auth_config_missing$",
    ):
        alpha_a_dev_cli.build_alpha_a_environment(dotenv_mapping=values)


@pytest.mark.parametrize(
    ("values", "error_code"),
    [
        (
            {
                alpha_a_dev_cli.PRODUCT_DATABASE_KEY: (
                    "mysql+asyncmy://user:secret@db.example/product"
                )
            },
            "vibe_market_morning_staging_database_url_missing",
        ),
        (
            _values(staging_database="market_morning_product"),
            "alpha_a_staging_database_name_invalid",
        ),
        (
            _values(
                product_database="market_morning_shared_staging",
                staging_database="market_morning_shared_staging",
            ),
            "alpha_a_staging_database_matches_product",
        ),
        (
            {
                **_values(),
                alpha_a_dev_cli.STAGING_DATABASE_KEY: (
                    "postgresql://staging:secret@db.example/"
                    "market_morning_alpha_a_staging"
                ),
            },
            "alpha_a_database_url_invalid",
        ),
    ],
)
def test_build_alpha_a_environment_rejects_unsafe_targets(
    values: dict[str, str],
    error_code: str,
) -> None:
    with pytest.raises(
        alpha_a_dev_cli.AlphaADevConfigurationError,
        match=f"^{error_code}$",
    ):
        alpha_a_dev_cli.build_alpha_a_environment(dotenv_mapping=values)


def test_check_only_emits_non_staging_evidence(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        alpha_a_dev_cli,
        "_load_alpha_a_environment",
        lambda _path: {"SAFE": "yes"},
    )

    assert alpha_a_dev_cli.main(["--check-only"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "counts_as_staging_day": False,
        "host": "127.0.0.1",
        "port": 8898,
        "runtime_enabled": False,
        "scope": "market_morning_alpha_a_local_staging",
        "status": "passed",
    }


def test_main_refuses_non_loopback_without_reading_env(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        alpha_a_dev_cli,
        "_load_alpha_a_environment",
        lambda _path: pytest.fail("environment must not be read"),
    )

    assert alpha_a_dev_cli.main(["--host", "0.0.0.0"]) == 2
    assert capsys.readouterr().err.strip() == "alpha_a_loopback_host_required"
