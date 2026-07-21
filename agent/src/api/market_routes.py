"""Authenticated REST route for the fixed global-index overview."""

from __future__ import annotations

import sys
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.api.system_routes import _SlidingWindowRateLimiter, _client_key
from src.config.accessor import get_env_config
from src.market_index_service import market_index_service


class MarketBar(BaseModel):
    session_date: str
    open: float
    high: float
    low: float
    close: float


class MarketIndexItem(BaseModel):
    symbol: str
    name: str
    name_ja: str
    market: Literal["global_index"]
    region: Literal["JP", "US"]
    currency: Literal["JPY", "USD"]
    timezone: str
    source: str
    delay_status: Literal["unknown", "eod", "delayed", "realtime"]
    tradable: Literal[False]
    window_start: str
    window_end: str
    latest: MarketBar
    previous_close: float | None
    change: float | None
    change_percent: float | None
    series: list[MarketBar]


class MarketIndexError(BaseModel):
    symbol: str
    provider: str
    code: Literal[
        "AMBIGUOUS_SYMBOL",
        "UNSUPPORTED_SYMBOL",
        "PROVIDER_UNAVAILABLE",
        "PROVIDER_RATE_LIMITED",
        "PROVIDER_SCHEMA_ERROR",
        "NO_DATA",
        "INVALID_RANGE",
    ]
    message: str


class MarketIndicesResponse(BaseModel):
    as_of: str
    range: Literal["1m", "3m", "6m", "1y"]
    interval: Literal["1D"]
    items: list[MarketIndexItem]
    errors: list[MarketIndexError]


_market_rate_limiter = _SlidingWindowRateLimiter(max_requests=60, window_seconds=60.0)


def _feature_enabled() -> bool:
    return get_env_config().api.market_indices_enabled


def register_market_routes(app: FastAPI) -> None:
    host = sys.modules.get("api_server") or sys.modules.get("agent.api_server")
    if host is None:
        raise RuntimeError("register_market_routes requires api_server to be imported")
    require_auth = host.require_auth

    @app.get(
        "/market/indices",
        response_model=MarketIndicesResponse,
        dependencies=[Depends(require_auth)],
    )
    async def get_market_indices(
        request: Request,
        range_name: Literal["1m", "3m", "6m", "1y"] = Query("1m", alias="range"),
    ):
        if not _feature_enabled():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Market indices feature is disabled",
            )
        if not _market_rate_limiter.allow(_client_key(request)):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded, try again later",
            )
        result = await market_index_service.get_indices(range_name)
        if not result["items"]:
            return JSONResponse(status_code=503, content=result)
        return result
