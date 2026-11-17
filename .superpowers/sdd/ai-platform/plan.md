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

## Follow-ups (deferred, bundle A)

- `agents` and `ingestion` egress is unrestricted; route them through allowlisting proxies too.
- Idempotency in `execution/order_manager.py` is in memory and one lock serialises all orders (bundle C).
- Redis has no persistence; decide when streams are used (bundle B).
- Squid image is pinned by tag, not digest; base images likewise.
- nginx serves plain HTTP; TLS termination is left to the host.
- `uvicorn[standard]` pulls `websockets`/`watchfiles` that nothing uses yet.
- Playwright is pinned to a version that does not support Ubuntu 26.04; upgrade it and unpin the e2e runner.
- Upstream CI was red for the same tick-size bug; the fix could be offered upstream.
