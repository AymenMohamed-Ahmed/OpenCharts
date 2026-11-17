# Platform (terminal + FastAPI backend)

The OpenCharts terminal is the front end. Behind it sit four FastAPI services, a
database, a cache and an egress proxy, all started by `docker compose`.

```
Browser ─► web (nginx, OpenCharts build) ─/api─► api (gateway + risk gate)
                                                  ├─► agents      LLM keys only
                                                  ├─► postgres, redis
                                                  └─► execution   broker keys only
                                                        └─► egress-proxy ─► Hyperliquid
ingestion ─► Hyperliquid market data, postgres, redis
```

## Layout

| Path | What |
|---|---|
| `backend/app/api` | Gateway. Auth, the risk gate (`risk/gate.py`), order entry. |
| `backend/app/agents` | Agents. They propose `OrderIntent`s and never talk to a broker. |
| `backend/app/ingestion` | Market-data sources and pipelines. |
| `backend/app/execution` | Brokers (`paper`, `hyperliquid`) and the order manager. |
| `backend/app/common` | Per-service settings, shared schemas, auth helper. |
| `backend/Dockerfile` | One image for all four services; `SERVICE` picks the app. |
| `infra/web` | nginx image that serves the terminal and proxies `/api`. |
| `infra/egress-proxy` | Squid config and the host allowlist. |
| `docker-compose.yml`, `setup.sh`, `.env.example` | Deployment. |

## Network rules

| Service | Networks | Can reach | Secrets it holds |
|---|---|---|---|
| `web` | `edge`, `web_net` | `api` | none |
| `api` | `web_net`, `agents_net`, `data_net`, `exec_net` | agents, execution, postgres, redis | API token, admin token, internal token, database |
| `agents` | `agents_net`, `llm_egress` | internet (LLM providers) | LLM keys |
| `ingestion` | `data_net`, `market_egress` | internet (market data), postgres, redis | database |
| `execution` | `exec_net` | `egress-proxy` only | broker key, internal token; the `execdata` volume (order ledger) |
| `egress-proxy` | `exec_net`, `broker_egress` | hosts in `allowlist.txt`, port 443 | none |
| `postgres`, `redis` | `data_net` | nothing | own password |

`web_net`, `agents_net`, `data_net` and `exec_net` are `internal: true`: no route to
the internet. Only `web` publishes a port, on `127.0.0.1` by default.

`backend/tests/test_compose_isolation.py` asserts these rules against
`docker-compose.yml`, and the `Backend` workflow probes them on a running stack.

Limits worth knowing:

- `llm_egress` and `market_egress` are ordinary bridge networks, so `agents` and
  `ingestion` can reach any host, not only their providers.
- The compose networks isolate containers from each other. They do not replace a
  host firewall or TLS; put a TLS-terminating proxy in front before binding
  anything other than loopback.

## Running it

```bash
./setup.sh
```

It checks Docker, creates `.env` from `.env.example`, generates the five internal
secrets, validates the broker settings, checks the port, builds and starts
everything, and waits for health checks. `./setup.sh --help` lists the options.

Defaults are safe: `BROKER=paper` (simulated fills), `TRADING_ENABLED=false` (the
gateway refuses every order), testnet, and trading halted until an operator
arms it (below).

## Orders and risk controls

Every order goes `POST /api/v1/orders` (API token) → risk gate → execution. The
gateway refuses an order, in this order, when:

1. `TRADING_ENABLED=false`, the symbol is not in `ALLOWED_SYMBOLS`, or the
   notional is above `MAX_ORDER_NOTIONAL_USD` (reduce-only orders are exempt);
2. the kill switch is on, or trading has never been armed (all orders);
3. account equity is `MAX_DAILY_LOSS_USD` or more below its value at 00:00 UTC,
   or equity cannot be read (non-reduce-only orders only).

Operator controls, with `ADMIN_TOKEN` (never the API token):

```bash
ADMIN="Authorization: Bearer $(grep '^ADMIN_TOKEN=' .env | cut -d= -f2)"
curl -X POST -H "$ADMIN" http://127.0.0.1:8080/api/v1/admin/resume   # arm trading
curl -X POST -H "$ADMIN" http://127.0.0.1:8080/api/v1/admin/kill \
     -H 'content-type: application/json' -d '{"reason":"manual stop"}'  # halt + cancel all
curl -H "$ADMIN" http://127.0.0.1:8080/api/v1/admin/status
```

The kill switch lives in Redis and fails closed: after a fresh start, or if
Redis loses its data (it has no persistence), trading stays halted until you
call `resume`, and while Redis is unreachable the gateway refuses orders. A
kill records the halt first, then cancels every open core-perp order on the
account, and again five seconds later. It does not close positions.

`GET /api/v1/orders/{client_order_id}` and `POST /api/v1/orders/{client_order_id}/cancel`
read and cancel an order; cancels are allowed while trading is halted.

### How execution places an order

- **Idempotency.** `client_order_id` is the key. Its Hyperliquid `cloid` is the
  first 16 bytes of SHA-256 of it. Retrying with the same id returns the same
  order; reusing an id for a different order is a 409.
- **Durability.** The order is written to the SQLite ledger on the `execdata`
  volume as `pending`, with its signed request, before it is sent. If the
  response is lost (timeout, restart), the status is `pending`; execution asks
  Hyperliquid for the cloid, resends the identical signed request while it is
  valid (its nonce can only be used once), and closes it as never placed once
  it has expired (30 s plus a minute of grace). Retry with the same id, or
  `GET` it, to read the outcome.
- **Pricing.** Market orders are IOC limits at the Hyperliquid mid plus or minus
  `MAX_SLIPPAGE_BPS`; the caller's `reference_price` must be within that
  distance of the mid. Limit orders are GTC, rounded to the tick in the
  caller's favour, and refused if marketable by more than `MAX_SLIPPAGE_BPS`.
  Sizes round down to the market's size step; non-reduce-only orders below
  $10 are refused.
- **Leverage and margin mode** are whatever the account has set for that
  market on Hyperliquid. Execution never changes them; set them in the
  Hyperliquid app before trading.
- **Scope:** core perps only. No TP/SL, modify, HIP-3 or spot.

## Hyperliquid testnet setup

The platform trades with an API ("agent") wallet: it can place and cancel
orders for your account but cannot withdraw. Never put the account's own
private key in `.env`; execution refuses to start if you do.

1. Use a wallet you control as the account (testnet only to begin with) and get
   testnet USDC from the faucet at https://app.hyperliquid-testnet.xyz/drip
   (check its current conditions there; it has required prior mainnet use).
2. On https://app.hyperliquid-testnet.xyz/API, connect that wallet, name a new
   API wallet and generate it, then authorize it and sign with the account
   wallet. Copy the API wallet's private key when it is shown; it is not shown
   again.
3. In `.env`:

   ```bash
   BROKER=hyperliquid
   PLATFORM_HL_NETWORK=testnet
   HL_ACCOUNT_ADDRESS=0x...        # the account (master) address, not the API wallet's
   HL_AGENT_PRIVATE_KEY=0x...      # the API wallet's private key
   TRADING_ENABLED=true
   MAX_ORDER_NOTIONAL_USD=20
   ALLOWED_SYMBOLS=BTC
   ```

4. `./setup.sh`, then `docker compose logs execution` should show
   `hyperliquid testnet: account 0x..., agent 0x...` and `asset map: N core perps`.
5. Arm trading (`/api/v1/admin/resume` above), check the account
   (`/api/v1/admin/status` shows `equity`), then place one small order through
   the gateway:

   ```bash
   API="Authorization: Bearer $(grep '^API_AUTH_TOKEN=' .env | cut -d= -f2)"
   curl -X POST -H "$API" -H 'content-type: application/json' \
     http://127.0.0.1:8080/api/v1/orders -d '{"client_order_id":"first-testnet-0001",
     "symbol":"BTC","side":"buy","quantity":<worth $10-20>,"order_type":"limit","limit_price":<about 2% below the mid>}'
   curl -H "$API" http://127.0.0.1:8080/api/v1/orders/first-testnet-0001
   curl -X POST -H "$API" http://127.0.0.1:8080/api/v1/orders/first-testnet-0001/cancel
   ```

   A resting limit below the market, then a cancel, exercises signing, the
   ledger and cancel without a fill. The order should appear (and disappear) in
   the Hyperliquid testnet app's open orders.

An API wallet can be approved with an expiry; an expired or revoked one makes
every order fail with "The API wallet is not authorised". Mainnet
needs `LIVE_TRADING=true` as well and a separate API wallet approved on mainnet.

## What is not built yet

The market-data stream, LLM-backed agents, database schema and the terminal
adapter that points OpenCharts at this gateway. The web container serves the
demo terminal until that adapter exists. See `.superpowers/sdd/ai-platform/plan.md`.
