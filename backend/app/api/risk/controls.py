"""Kill switch and daily-loss limit, kept in Redis.

Kill switch: `risk:trading` holds "armed" or "killed". It fails CLOSED: when
the key is missing (a fresh deployment, or Redis lost its data, which it does
on restart because it has no persistence) trading is halted until an operator
calls `POST /v1/admin/resume`. It sits on top of TRADING_ENABLED: both must
allow trading.

Daily-loss limit: `risk:equity:<YYYY-MM-DD>` holds the day's starting equity
(UTC), written once per day (SET NX) at 00:00 by a background task, or by the
first order of the day if that task has not run. New non-reduce-only orders are
refused while equity is MAX_DAILY_LOSS_USD or more below it.

If Redis cannot be read, the gateway refuses orders.
"""

import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis

TRADING_KEY = "risk:trading"
DAY_KEY_PREFIX = "risk:equity:"
DAY_KEY_TTL_S = 3 * 24 * 3600


@dataclass(frozen=True)
class TradingState:
    armed: bool
    reason: str | None = None
    since: str | None = None

    @property
    def label(self) -> str:
        if self.armed:
            return "armed"
        return "killed" if self.since else "not armed"


class RiskStateUnavailable(Exception):  # noqa: N818
    pass


def utc_day(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")


class RiskControls:
    def __init__(self, redis: Redis, clock=time.time) -> None:
        self._redis = redis
        self._clock = clock

    async def aclose(self) -> None:
        await self._redis.aclose()

    async def _call(self, coro):
        try:
            return await coro
        except Exception as exc:
            raise RiskStateUnavailable(
                f"risk state store unavailable ({type(exc).__name__})"
            ) from None

    async def trading_state(self) -> TradingState:
        raw = await self._call(self._redis.get(TRADING_KEY))
        if raw is None:
            return TradingState(False, "trading has not been armed; POST /v1/admin/resume")
        try:
            data = json.loads(raw)
        except ValueError:
            data = {}
        if data.get("state") == "armed":
            return TradingState(True, since=data.get("at"))
        return TradingState(False, data.get("reason") or "kill switch is on", data.get("at"))

    async def _set(self, state: str, reason: str | None) -> TradingState:
        at = datetime.fromtimestamp(self._clock(), UTC).isoformat()
        value = json.dumps({"state": state, "reason": reason, "at": at})
        await self._call(self._redis.set(TRADING_KEY, value))
        return await self.trading_state()

    async def kill(self, reason: str | None) -> TradingState:
        return await self._set(
            "killed", f"kill switch: {reason}" if reason else "kill switch is on"
        )

    async def resume(self) -> TradingState:
        return await self._set("armed", None)

    def today(self) -> str:
        return utc_day(self._clock())

    async def day_baseline(self) -> float | None:
        raw = await self._call(self._redis.get(DAY_KEY_PREFIX + self.today()))
        if raw is None:
            return None
        try:
            baseline = float(raw)
        except ValueError:
            baseline = math.nan
        if not math.isfinite(baseline):
            raise RiskStateUnavailable("daily equity snapshot is unreadable")
        return baseline

    async def snapshot_baseline(self, equity: float) -> float:
        """Record `equity` as today's start unless one exists; return the start."""
        key = DAY_KEY_PREFIX + self.today()
        await self._call(self._redis.set(key, repr(float(equity)), nx=True, ex=DAY_KEY_TTL_S))
        baseline = await self.day_baseline()
        if baseline is None:
            raise RiskStateUnavailable("daily equity snapshot could not be stored")
        return baseline
