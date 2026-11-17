import asyncio
import json

import httpx
import pytest
import websockets

from app.common.config import IngestionSettings
from app.ingestion.sources.base import Candle, Reconnected, Tick
from app.ingestion.sources.hyperliquid import (
    HL_MAX_CANDLES,
    HyperliquidClient,
    HyperliquidError,
    HyperliquidSource,
    parse_candle,
)


async def _no_sleep(_: float) -> None: ...


def raw(t: int, coin="BTC", interval="1m", **kw):
    return {
        "t": t,
        "T": t + 59_999,
        "s": coin,
        "i": interval,
        "o": "1",
        "c": "2",
        "h": "3",
        "l": "0.5",
        "v": "10",
        "n": 4,
        **kw,
    }


def make_client(handler, **kw) -> HyperliquidClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HyperliquidClient("https://hl.test", client=http, sleep=_no_sleep, **kw)


def test_parse_candle_maps_fields_and_rejects_garbage():
    assert parse_candle(raw(60_000)) == Candle("BTC", "1m", 60_000, 1.0, 3.0, 0.5, 2.0, 10.0, 4)
    assert parse_candle(raw(60_000, v="oops")).volume == 0.0
    assert parse_candle(raw(60_000, o="nan")) is None
    assert parse_candle({"t": 1}) is None


async def test_meta_assigns_indexes_and_skips_delisted():
    def handler(request):
        assert json.loads(request.content) == {"type": "meta"}
        return httpx.Response(
            200,
            json={
                "universe": [
                    {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
                    {"name": "OLD", "szDecimals": 1, "maxLeverage": 3, "isDelisted": True},
                    {"name": "ETH", "szDecimals": 4, "maxLeverage": 25},
                ]
            },
        )

    markets = await make_client(handler).meta()
    assert [(m.coin, m.asset_index, m.sz_decimals, m.max_leverage) for m in markets] == [
        ("BTC", 0, 5, 40),
        ("ETH", 2, 4, 25),
    ]


async def test_retries_429_and_5xx_then_succeeds():
    codes = iter([429, 503, 200])
    calls = []

    def handler(request):
        calls.append(1)
        code = next(codes)
        return httpx.Response(code, json={"universe": []})

    assert await make_client(handler).meta() == []
    assert len(calls) == 3


async def test_gives_up_after_max_attempts_and_does_not_retry_4xx():
    calls = []

    def always_500(request):
        calls.append(1)
        return httpx.Response(500)

    with pytest.raises(HyperliquidError):
        await make_client(always_500, max_attempts=3).meta()
    assert len(calls) == 3

    calls.clear()

    def bad_request(request):
        calls.append(1)
        return httpx.Response(422, text="nope")

    with pytest.raises(HyperliquidError):
        await make_client(bad_request).meta()
    assert len(calls) == 1


async def test_candles_page_past_the_5000_limit():
    step = 60_000
    total = HL_MAX_CANDLES + 10
    requests = []

    def handler(request):
        req = json.loads(request.content)["req"]
        requests.append(req)
        start, end = req["startTime"], req["endTime"]
        times = [start + i * step for i in range(HL_MAX_CANDLES) if start + i * step <= end]
        return httpx.Response(200, json=[raw(t) for t in times])

    candles = await make_client(handler).candles("BTC", "1m", 0, (total - 1) * step)
    assert len(candles) == total
    assert [c.open_time_ms for c in candles] == [i * step for i in range(total)]
    assert len(requests) == 2


async def test_candles_reject_unknown_interval():
    with pytest.raises(ValueError):
        await make_client(lambda r: httpx.Response(200, json=[])).candles("BTC", "7m", 0, 1)


# ---- websocket, against a local server ----------------------------------------------


class FakeHyperliquid:
    """Local websocket server; each connection replays `script[n]` after subscribing."""

    def __init__(self, scripts: list[list[dict]]) -> None:
        self.scripts = scripts
        self.connections = 0
        self.subscriptions: list[list[dict]] = []

    async def handler(self, ws):
        n = self.connections
        self.connections += 1
        subs: list[dict] = []
        self.subscriptions.append(subs)
        for _ in range(3):  # bbo BTC, candle BTC 1m, candle BTC 1h
            msg = json.loads(await ws.recv())
            assert msg["method"] == "subscribe"
            subs.append(msg["subscription"])
        for frame in self.scripts[n]:
            await ws.send(json.dumps(frame))
        await ws.close()  # force a reconnect


@pytest.fixture
async def source_factory():
    servers = []

    async def make(scripts):
        fake = FakeHyperliquid(scripts)
        server = await websockets.serve(fake.handler, "127.0.0.1", 0)
        servers.append(server)
        port = server.sockets[0].getsockname()[1]
        src = HyperliquidSource(
            IngestionSettings(), ws_url=f"ws://127.0.0.1:{port}", sleep=_no_sleep
        )
        return fake, src

    yield make
    for s in servers:
        s.close()
        await s.wait_closed()


async def collect(source, n: int):
    out = []

    async def run():
        async for ev in source.stream(["BTC"], ["1m", "1h"]):
            out.append(ev)
            if len(out) >= n:
                return

    await asyncio.wait_for(run(), timeout=10)
    return out


async def test_stream_subscribes_decodes_and_reconnects(source_factory):
    bbo = {
        "channel": "bbo",
        "data": {"coin": "BTC", "time": 1000, "bbo": [{"px": "101"}, {"px": "100"}]},
    }
    candle_obj = {"channel": "candle", "data": raw(60_000)}
    candle_list = {"channel": "candle", "data": [raw(120_000, interval="1h")]}
    noise = [
        {"channel": "subscriptionResponse", "data": {}},
        {"channel": "bbo", "data": {"coin": "SOL", "time": 1, "bbo": [{"px": "1"}, {"px": "2"}]}},
        {"channel": "candle", "data": raw(1, coin="SOL")},
        {"channel": "bbo", "data": {"coin": "BTC", "time": 1, "bbo": [None, {"px": "2"}]}},
    ]
    fake, src = await source_factory([[*noise, bbo, candle_obj, candle_list], [bbo]])
    events = await collect(src, 7)

    assert events[0] == Reconnected()
    assert events[1] == Tick("BTC", 100.0, 101.0, 1000)  # crossed sides are ordered
    assert events[2].open_time_ms == 60_000 and events[2].interval == "1m"
    assert events[3].interval == "1h"
    assert events[4] == Reconnected()  # second connection
    assert events[5] == Tick("BTC", 100.0, 101.0, 1000)
    assert fake.connections >= 2
    assert fake.subscriptions[0] == [
        {"type": "bbo", "coin": "BTC"},
        {"type": "candle", "coin": "BTC", "interval": "1m"},
        {"type": "candle", "coin": "BTC", "interval": "1h"},
    ]


async def test_stream_retries_when_the_server_is_down():
    src = HyperliquidSource(IngestionSettings(), ws_url="ws://127.0.0.1:1", sleep=_no_sleep)
    gen = src.stream(["BTC"], ["1m"])
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(gen.__anext__(), timeout=0.5)  # keeps retrying, never raises
