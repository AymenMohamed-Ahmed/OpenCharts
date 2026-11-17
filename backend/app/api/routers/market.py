"""Market-data reads for the terminal and the agents. Postgres and Redis only."""

import re
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.common.auth import require_bearer
from app.common.config import ApiSettings, api_settings
from app.ingestion.sources.hyperliquid import INTERVAL_MS

router = APIRouter(prefix="/v1/market", tags=["market"])
_bearer = HTTPBearer(auto_error=False)
_COIN = re.compile(r"^[A-Za-z0-9:._-]{1,32}$")
MAX_CANDLES = 5000
MAX_QUOTE_COINS = 50


def _authenticated(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[ApiSettings, Depends(api_settings)],
) -> None:
    require_bearer(credentials, settings.api_auth_token)


Auth = Depends(_authenticated)


class Symbol(BaseModel):
    coin: str
    asset_index: int
    sz_decimals: int
    max_leverage: int
    updated_at: datetime


class CandleOut(BaseModel):
    time: int  # open time, epoch milliseconds
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: int


class QuoteOut(BaseModel):
    coin: str
    bid: float
    ask: float
    time: int  # epoch milliseconds


def _pool(request: Request) -> Any:
    if request.app.state.db_pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database not configured")
    return request.app.state.db_pool


@router.get("/symbols", dependencies=[Auth])
async def symbols(request: Request) -> list[Symbol]:
    async with _pool(request).connection() as conn:
        cur = await conn.execute(
            "SELECT coin, asset_index, sz_decimals, max_leverage, updated_at "
            "FROM markets ORDER BY asset_index"
        )
        return [Symbol(**dict(zip(Symbol.model_fields, row, strict=True))) async for row in cur]


@router.get("/candles", dependencies=[Auth])
async def candles(
    request: Request,
    coin: Annotated[str, Query(pattern=_COIN.pattern)],
    interval: str,
    from_: Annotated[
        int | None, Query(alias="from", ge=0, description="epoch ms, inclusive")
    ] = None,
    to: Annotated[int | None, Query(ge=0, description="epoch ms, inclusive")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_CANDLES)] = 500,
) -> list[CandleOut]:
    """The newest `limit` candles inside [from, to], oldest first."""
    if interval not in INTERVAL_MS:
        raise HTTPException(422, f"interval must be one of {', '.join(INTERVAL_MS)}")
    lo = datetime.fromtimestamp((from_ or 0) / 1000, tz=UTC)
    hi = datetime.fromtimestamp(to / 1000, tz=UTC) if to is not None else None
    async with _pool(request).connection() as conn:
        cur = await conn.execute(
            "SELECT * FROM (SELECT open_time, o, h, l, c, v, trades FROM candles "
            'WHERE coin = %s AND "interval" = %s AND open_time >= %s '
            "AND (%s::timestamptz IS NULL OR open_time <= %s) "
            "ORDER BY open_time DESC LIMIT %s) newest ORDER BY open_time",
            (coin, interval, lo, hi, hi, limit),
        )
        return [
            CandleOut(
                time=int(t.timestamp() * 1000), open=o, high=h, low=lo_, close=c, volume=v, trades=n
            )
            async for t, o, h, lo_, c, v, n in cur
        ]


@router.get("/quotes", dependencies=[Auth])
async def quotes(
    request: Request,
    coins: Annotated[str, Query(description="comma-separated, e.g. BTC,ETH")],
) -> list[QuoteOut]:
    names = list(dict.fromkeys(c.strip() for c in coins.split(",") if c.strip()))
    if not names or len(names) > MAX_QUOTE_COINS or not all(_COIN.match(c) for c in names):
        raise HTTPException(422, f"give 1 to {MAX_QUOTE_COINS} valid coins")
    redis = request.app.state.redis
    if redis is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "redis not configured")
    async with redis.pipeline(transaction=False) as pipe:
        for c in names:
            pipe.hgetall(f"quote:{c}")
        rows = await pipe.execute()
    out: list[QuoteOut] = []
    for coin, row in zip(names, rows, strict=True):
        if not row:
            continue
        f = {_s(k): _s(v) for k, v in row.items()}
        out.append(
            QuoteOut(coin=coin, bid=float(f["bid"]), ask=float(f["ask"]), time=int(float(f["ts"])))
        )
    return out


def _s(v: Any) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)
