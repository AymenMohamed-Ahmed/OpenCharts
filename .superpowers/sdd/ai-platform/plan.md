# AI trading platform on OpenCharts: plan

Start here in a new session. Update this file as decisions land.

## Decisions

- Ruling: OpenCharts is the front end; a Python FastAPI backend sits behind it — the user chose this over an all-TypeScript build — cost if wrong: backend rewrite.
- Ruling: server-side broker is Hyperliquid, signed with an API (agent) wallet key — matches the terminal's symbols and account model; an agent key cannot withdraw — cost if wrong: new broker client only (`execution/broker/`).
- Ruling: execution has no database access and no direct internet route; egress only through the Squid allowlist — keeps the blast radius of the broker key small — cost if wrong: order history must flow through the gateway.
- Ruling: agents never reach execution; every order passes the gateway risk gate — an LLM-produced order must not skip checks — cost if wrong: none acceptable.
- Ruling: platform variables live in the root `.env` next to the terminal's; compose passes each container an explicit list — one file for the operator, no `env_file` — cost if wrong: move to per-service files.
- Ruling: integration branch is `dev`; nothing goes to `main` without an explicit ask.
- Ruling: `PLATFORM_HL_NETWORK` (not `HL_NETWORK`) selects the backend network — `HL_NETWORK` is already used by the builder scripts and defaults to mainnet — cost if wrong: rename one variable.

- Ruling: bundles B and C run in parallel sessions, each branched from `feat/platform-foundation` with its PR into `dev` (stacked on PR 1 until it merges) — they touch different services and need no shared test DB — cost if wrong: one merge round on the shared files listed under "Parallel work".
- Ruling: execution keeps its own durable ledger in SQLite on a dedicated volume, not in Postgres — preserves "execution has no database network" and keeps C independent of B — cost if wrong: migrate the ledger into Postgres later.
- Ruling: Postgres schema is plain numbered SQL files applied by a one-shot `migrate` compose service (psycopg 3), not Alembic — no ORM in the project, fewer dependencies — cost if wrong: adopt Alembic and baseline the existing schema.
- Ruling: v1 trades and ingests core Hyperliquid perps only (no HIP-3 builder dexes, no spot) — cuts scope; the terminal already handles HIP-3 itself — cost if wrong: extend the asset map with `perpDexs`.
- Ruling: agent orders carry no builder code — it is the operator's own account — cost if wrong: none.
- Ruling: Hyperliquid signing uses the official `hyperliquid-python-sdk` signing functions, never hand-written crypto — signing bugs lose money silently — cost if wrong: SDK dependency weight only.

## Conventions

- Backend: Python 3.12, uv, ruff (lint + format), pytest. From `backend/`: `uv sync`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest -q`.
- Front end: the repo's existing checks (`npm run typecheck`, `lint -- --max-warnings=0`, `test`, `build`).
- Any change to `docker-compose.yml` must keep `backend/tests/test_compose_isolation.py` passing; extend it when adding a service or network.
- PRs go into `dev`, one review and one fix round each.

## Bundles

| # | Bundle | Layer | Risk | Depends on | State |
|---|---|---|---|---|---|
| A | Foundation: CI fix, backend scaffold, Docker, compose, env, setup.sh | all | medium | none | pushed; PR 1 into `dev` |
| B | Ingestion: Hyperliquid REST + websocket, TimescaleDB schema and migrations, Redis fan-out | backend | low | A | not started |
| C | Execution: Hyperliquid signing and order placement, durable idempotency, order/fill store via gateway, daily-loss limit and kill switch | backend | high (money, keys) | A | not started |
| D | Agents: LLM client, first strategy, propose → gateway flow, agent read access to market data | backend | medium | A, B | not started |
| E | Terminal adapter: `OpenChartsAdapter` backed by the gateway (market data, trading, storage), token handling in the browser | front end | medium | B, C | not started |

B and C are independent of each other and may run in parallel sessions. C gets the high-risk review.

## Bundle A: state

Branch `feat/platform-foundation`, cut from `dev` (= fork `main` at 399e275).

Contents: tick-size float fix (`src/hyperliquid/symbols.ts`, `orders.ts`), `backend/`, `infra/`, `docker-compose.yml`, `setup.sh`, `.env.example` platform section, `.github/workflows/backend.yml`, Pages deploy guarded on forks, `docs/PLATFORM.md`.

Verified locally: front-end typecheck, lint, 745 tests, build; backend ruff and 52 tests; `./setup.sh` from clean, re-run, `--check`, port conflict with and without `--auto-port`, non-loopback bind refusal, hyperliquid-without-credentials refusal; on the running stack: order path through nginx, risk rejections, agents cannot resolve execution or postgres, execution cannot reach the internet directly, proxy refuses a non-allowlisted host.

Verified on GitHub runners (PR 1): the same stack checks, plus an allowlisted Hyperliquid testnet call through the proxy, which the build sandbox could not do (no external DNS in containers).

Also fixed in this bundle: the browser e2e workflow (runner pinned to ubuntu-24.04 because the locked Playwright cannot install Chromium on 26.04; one ambiguous locator in `e2e/onboarding.spec.ts` that failed about one run in three).

To push: `git push -u origin dev feat/platform-foundation`, then open a PR from `feat/platform-foundation` into `dev`.

## Parallel work (B and C at the same time)

Shared files and who owns what:

- `backend/app/common/config.py`: B edits only `IngestionSettings` and the read side of `ApiSettings` it needs; C edits only `ExecutionSettings` and the risk fields of `ApiSettings`.
- `docker-compose.yml`, `.env.example`, `backend/tests/test_compose_isolation.py`, `docs/PLATFORM.md`: each bundle edits only its own services' blocks and sections, and extends the isolation test for anything it adds.
- `backend/pyproject.toml`, `backend/uv.lock`: whoever merges second merges `dev` in and re-runs `uv lock`; never hand-edit the lock.
- This file: each bundle edits only its own "Bundle X: state" and "Follow-ups (bundle X)" sections.

## Sandbox notes (cloud sessions)

- Docker: the daemon is not running at start. Start it with `dockerd > <scratchpad>/dockerd.log 2>&1 &`.
- Image builds cannot reach PyPI or npm because containers do not trust the session proxy CA. Do not change the repo's Dockerfiles for this. Build with a scratchpad-only compose override that adds the CA: copy `/root/.ccr/ca-bundle.crt` into `<scratchpad>/ca/ca.crt`; generate copies of `backend/Dockerfile` and `infra/web/Dockerfile` with, after each `FROM python...` / `FROM node...` line, `COPY --from=sandboxca ca.crt /sandbox-ca.crt` and `ENV SSL_CERT_FILE=/sandbox-ca.crt PIP_CERT=/sandbox-ca.crt REQUESTS_CA_BUNDLE=/sandbox-ca.crt NODE_EXTRA_CA_CERTS=/sandbox-ca.crt npm_config_cafile=/sandbox-ca.crt`; point an override file's `build.dockerfile` at them with `additional_contexts: {sandboxca: <scratchpad>/ca}`; then `export COMPOSE_FILE=docker-compose.yml:<override> BUILDX_BAKE_ENTITLEMENTS_FS=0` and run `./setup.sh`.
- Containers get no external DNS in the sandbox, so anything that talks to Hyperliquid from inside a container is verified by the `Backend` workflow on GitHub, not locally. Unit tests use fakes.
- `curl` to the local stack needs `--noproxy 127.0.0.1`.
- `gh pr create` fails (GraphQL is blocked); open PRs with `gh api repos/AymenMohamed-Ahmed/OpenCharts/pulls -f title=... -f base=dev -f head=<branch> -F body=@<file>`.
- The user-level agents (`lean-implementer`, `lean-reviewer`), the SDD handbook and graphify from the user's CLAUDE.md live on their Windows machine, not here. Work inline; keep this plan current instead of a separate ledger.

## Handoff: bundle B (ingestion)

Branch `feat/ingestion` from `feat/platform-foundation`; PR into `dev`, body notes it is stacked on PR 1. Risk: low (Sonnet review + CI).

Goal: live Hyperliquid market data stored and served by the gateway, so agents (bundle D) and the terminal adapter (bundle E) have one source.

Scope:

1. **Schema and migrations.** `backend/migrations/NNNN_*.sql`, a runner `backend/app/common/migrate.py` (table `schema_migrations`, each file applied once in a transaction, idempotent re-run), and a one-shot `migrate` compose service (backend image, `data_net` only, `restart: "no"`); `api` and `ingestion` depend on it with `condition: service_completed_successfully`. Tables: `markets` (coin, asset_index, sz_decimals, max_leverage, updated_at), `candles` hypertable (coin, interval, open_time, o, h, l, c, v, trades; PK coin+interval+open_time; upsert), `quotes` hypertable (coin, ts, bid, ask; append, with a retention policy, default 7 days).
2. **Hyperliquid REST** in `app/ingestion/sources/hyperliquid.py`: `meta` for the market list, `candleSnapshot` backfill (max 5000 candles per call, see `src/hyperliquid/marketData.ts:46`; `toCandle` at `marketData.ts:69` shows the field mapping). Retry with backoff on 429/5xx.
3. **Hyperliquid websocket**: `bbo` and `candle` subscriptions for configured coins (formats at `src/hyperliquid/marketData.ts:172` and `:255-275`; limits at `src/hyperliquid/ws.ts:23-24`). Reconnect with backoff and resubscribe; after a reconnect, backfill the gap via REST.
4. **Pipelines**: write candles and quotes to Postgres in batches; publish the latest quote per coin to Redis (`quote:<coin>` hash plus a `quotes` pub/sub channel).
5. **Gateway read endpoints** (same bearer token as orders): `GET /v1/market/symbols`, `GET /v1/market/candles?coin=&interval=&from=&to=&limit=`, `GET /v1/market/quotes?coins=`. Read from Postgres and Redis; no Hyperliquid calls from `api`.
6. Settings: `INGEST_COINS` (default `BTC,ETH`), `INGEST_INTERVALS` (default `1m,1h`), `INGEST_BACKFILL_DAYS` (default 7), `QUOTE_RETENTION_DAYS` (default 7). Add to `.env.example`.
7. Tests: unit tests with a fake HTTP transport and a local fake websocket server; migration runner against a real Postgres in the `Backend` workflow (a `services: postgres` container or the compose stack). Extend the stack job to wait for one candle row for BTC on testnet (non-blocking, like the proxy check).

Out of scope: HIP-3 dexes, spot, order books (`l2Book`), trades tape, streaming quotes to the browser (bundle E).

Dependencies to add: `psycopg[binary]`, `websockets` (explicit), `redis`.

## Handoff: bundle C (execution)

Branch `feat/execution` from `feat/platform-foundation`; PR into `dev`, body notes it is stacked on PR 1. Risk: high (money, keys): Opus review before the PR is marked ready, plus CI.

Goal: the execution service places, tracks and cancels real Hyperliquid orders safely, and the gateway can stop trading at once.

Scope:

1. **Asset map**: on start and every 10 minutes, `meta` via the proxy → coin → asset index, `szDecimals`, `maxLeverage`. Core perps only.
2. **Price and size rules**, ported from the terminal with the same test cases: `src/hyperliquid/symbols.ts:35-66` (`allowedPriceDecimals`, `formatHlPrice`, `formatHlSize`), `src/hyperliquid/orders.ts:83-117` (`formatPx`, `formatSz`, `slippagePx`, `minSizeFor`), minimum notional $10 unless reduce-only (`orders.ts:21`). Port the matching cases from `src/hyperliquid/__tests__/symbols.test.ts` and `orders.test.ts`.
3. **Order placement** in `app/execution/broker/hyperliquid.py`: limit orders as GTC; market orders as IOC limit orders at `slippagePx` with `MAX_SLIPPAGE_BPS` (default 300, matches `orders.ts:19`). `cloid` derived deterministically from `client_order_id` (first 16 bytes of SHA-256, `0x`-hex), so a retry or a restart can find the order. Signing with the SDK's `sign_l1_action` (or its `Exchange` class) using the agent key; requests go through `HTTPS_PROXY`. Map responses and errors like `parseOrderResponse` at `orders.ts:450-511`; never log the key (see `redactSecrets` at `orders.ts:512`).
4. **Durable ledger**: SQLite at `/data/ledger.db` on a new named volume `execdata` (the only writable path besides `/tmp`). Record `pending` before sending, then the result. On start and on a timeout, reconcile `pending` rows with `orderStatus` by `cloid`. Replace the single global lock with a per-`client_order_id` lock.
5. **Endpoints** (internal token): `POST /internal/orders` (exists), `POST /internal/orders/{client_order_id}/cancel`, `POST /internal/cancel-all`, `GET /internal/orders/{client_order_id}`, `GET /internal/account` (equity, margin used, positions, from `clearinghouseState`; `src/hyperliquid/accountState.ts:53` shows the fields).
6. **Gateway controls**: a kill switch (`POST /v1/admin/kill` and `/v1/admin/resume`, separate `ADMIN_TOKEN`) stored in Redis so it survives restarts and wins over `TRADING_ENABLED`; kill also calls execution `cancel-all`. A daily-loss limit `MAX_DAILY_LOSS_USD`: the gateway snapshots equity from `/internal/account` at 00:00 UTC (or first order of the day) in Redis and refuses new non-reduce-only orders once equity has fallen by that amount. Gateway proxies cancel and order status as `/v1/orders/{id}/cancel` and `GET /v1/orders/{id}`.
7. **Tests**: a fake Hyperliquid exchange (in-process, httpx transport) that recovers the signer from each signed action with `eth_account` and asserts it equals the agent address, checks the asset index, price and size strings, and `cloid`; crash-recovery test (pending row → reconcile); idempotency under concurrent retries; kill switch and daily-loss tests. No test touches a real network.
8. Docs: in `docs/PLATFORM.md`, how to create and approve an API wallet on Hyperliquid testnet and fill `HL_ACCOUNT_ADDRESS` / `HL_AGENT_PRIVATE_KEY`; the user runs the first live testnet order themselves (the session has no testnet account).

Out of scope: TP/SL, leverage and margin-mode changes (orders use the account's current setting; document it), modify-order, HIP-3, spot, Postgres order history (after B, in E).

Dependencies to add: `hyperliquid-python-sdk` (0.24.x; pins `eth-account<0.14`), `eth-account` for the fake exchange in tests.

## Follow-ups (deferred, bundle A)

- `agents` and `ingestion` egress is unrestricted; route them through allowlisting proxies too.
- Idempotency in `execution/order_manager.py` is in memory and one lock serialises all orders (bundle C).
- Redis has no persistence; decide when streams are used (bundle B).
- Squid image is pinned by tag, not digest; base images likewise.
- nginx serves plain HTTP; TLS termination is left to the host.
- `uvicorn[standard]` pulls `websockets`/`watchfiles` that nothing uses yet.
- Playwright is pinned to a version that does not support Ubuntu 26.04; upgrade it and unpin the e2e runner.
- Upstream CI was red for the same tick-size bug; the fix could be offered upstream.

## Bundle C: state

Branch `feat/execution`, cut from `feat/platform-foundation`; PR into `dev`, stacked on PR 1.

Contents:

- `execution/broker/hl_rules.py`: price/size rules ported from the terminal in `Decimal`, with the terminal's test cases (`tests/test_hl_rules.py`).
- `execution/broker/hyperliquid.py`: asset map from `meta` (start + every 10 min), market = IOC at mid ± `MAX_SLIPPAGE_BPS`, limit = GTC rounded in the caller's favour, price band against the mid (a market order's `reference_price` and a marketable limit price must be within `MAX_SLIPPAGE_BPS`), $10 minimum, SDK `sign_l1_action` with `expiresAfter` = now + 30 s, cancel by cloid, cancel-all (core perps), account from `clearinghouseState`. Refuses the account's own key as agent key. `hl_errors.py`: humanised, redacted errors.
- `execution/ledger.py` (SQLite on `execdata`, schema version 1) and `order_manager.py`: per-id locks, `pending` row with the exact signed request before sending, reconciliation by cloid in a background loop (also at start), resend of the identical request while valid (nonce makes it at-most-once), closed as never placed after expiry + 60 s grace. Rows are tied to their venue (broker, network, account). Reused id for a different order → 409.
- Gateway: kill switch / resume / status (`ADMIN_TOKEN`) in Redis, failing closed (no recorded state or Redis unreachable → no orders); kill records the halt, then cancel-all, plus a second pass after 5 s; cancel-all also stops resends of pending orders. Daily-loss limit with a 00:00 UTC snapshot (or first order). `GET /v1/orders/{id}`, `POST /v1/orders/{id}/cancel`.
- Deployment: `execdata` volume, `/data` in the image, `ADMIN_TOKEN` generated by `setup.sh`, `.env.example`, isolation test (admin token only in `api`, `execdata` only in `execution`), stack job checks the kill switch and ledger persistence. `docs/PLATFORM.md`: controls, order handling, testnet API wallet setup.
- Ruling taken in this bundle: the kill switch fails closed when its Redis key is missing (Redis has no persistence), so trading needs one `resume` after a fresh start or a Redis restart.

Verified locally: ruff, format, 204 backend tests (fake exchange recovers the signer with eth_account and recomputes the action hash independently of the SDK); `./setup.sh` with the sandbox override (the last run, on the final commit, built the backend image with the classic builder and started it with `docker compose up --no-build`, because Docker Hub rate-limited BuildKit's base-image lookups; `./setup.sh --check` passed); on the running stack: order refusals, kill/resume/status through nginx, paper order through the gateway, ledger survives an execution restart, trading halted after a Redis restart until resume, orders refused while Redis is down, execution has no direct route out, agents cannot reach execution.

Not verified: any real Hyperliquid call (no testnet account in the session, no external DNS in containers). The user runs the first live testnet order (steps in `docs/PLATFORM.md`, "Hyperliquid testnet setup").

## Follow-ups (bundle C)

- Daily-loss equity is `marginSummary.accountValue`: wrong for unified accounts (perp slice only, see `accountState.ts`) and moved by deposits or withdrawals. Consider `portfolio` history or netting transfers.
- The day's starting equity is lost when Redis loses its data; after the required `resume`, the next order snapshots current equity, forgetting the loss so far that day.
- Concurrent orders can pass the daily-loss check together; the per-order cap bounds it.
- Min notional for a market sell is checked at the mid; Hyperliquid may check at the (lower) IOC price and reject orders just above $10.
- A fill found by reconciliation has no average price (`orderStatus` has none); fetch it from `userFills`. A reconciled partially filled IOC shows as `canceled` with `filled_quantity`.
- Kill does not cancel spot or HIP-3 orders (reported as `skipped`) and does not close positions.
- The first gateway call after a Redis restart fails with 503 (stale connection); add a retry.
- No pruning of the ledger; no alert when the agent wallet expires or is revoked.
- `allMids` is fetched per order; cache it if order rates grow.
- Paper equity is constant, so the loss limit is never exercised in paper mode.
- `pyproject.toml` / `uv.lock`: both B and C add `redis`; the second to merge re-runs `uv lock`.
