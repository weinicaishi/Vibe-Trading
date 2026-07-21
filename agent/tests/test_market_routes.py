from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import api_server
from src.api import market_routes
from src.config.accessor import reset_env_config


class _Service:
    def __init__(self, result):
        self.result = result

    async def get_indices(self, range_name):
        result = dict(self.result)
        result["range"] = range_name
        return result


def _result(items=None, errors=None):
    return {
        "as_of": "2026-07-17T08:00:00Z",
        "range": "1m",
        "interval": "1D",
        "items": items or [],
        "errors": errors or [],
    }


@pytest.fixture(autouse=True)
def _reset_config_cache() -> Iterator[None]:
    reset_env_config()
    yield
    reset_env_config()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("VIBE_MARKET_INDICES_ENABLED", "true")
    reset_env_config()
    monkeypatch.setattr(api_server, "_API_KEY", "")
    market_routes._market_rate_limiter.reset()
    return TestClient(api_server.app, client=("127.0.0.1", 50000))


def test_invalid_range_returns_422(client: TestClient) -> None:
    assert client.get("/market/indices", params={"range": "5m"}).status_code == 422


def test_feature_flag_defaults_closed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBE_MARKET_INDICES_ENABLED", "false")
    reset_env_config()
    response = client.get("/market/indices")
    assert response.status_code == 503
    assert response.json()["detail"] == "Market indices feature is disabled"


def test_all_failure_returns_503_body(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    error = {
        "symbol": "NIKKEI225.INDEX",
        "provider": "yahoo",
        "code": "NO_DATA",
        "message": "Market data is temporarily unavailable.",
    }
    monkeypatch.setattr(market_routes, "market_index_service", _Service(_result(errors=[error])))
    response = client.get("/market/indices")
    assert response.status_code == 503
    assert response.json()["errors"][0]["code"] == "NO_DATA"


def test_remote_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBE_MARKET_INDICES_ENABLED", "true")
    reset_env_config()
    monkeypatch.setattr(api_server, "_API_KEY", "secret")
    remote = TestClient(api_server.app, client=("203.0.113.9", 51000))
    assert remote.get("/market/indices").status_code == 401
