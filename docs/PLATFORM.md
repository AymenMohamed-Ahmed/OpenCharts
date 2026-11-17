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
| `api` | `web_net`, `agents_net`, `data_net`, `exec_net` | agents, execution, postgres, redis | API token, internal token, database |
| `agents` | `agents_net`, `llm_egress` | internet (LLM providers) | LLM keys |
| `ingestion` | `data_net`, `market_egress` | internet (market data), postgres, redis | database |
| `execution` | `exec_net` | `egress-proxy` only | broker key, internal token |
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

It checks Docker, creates `.env` from `.env.example`, generates the four internal
secrets, validates the broker settings, checks the port, builds and starts
everything, and waits for health checks. `./setup.sh --help` lists the options.

Defaults are safe: `BROKER=paper` (simulated fills), `TRADING_ENABLED=false` (the
gateway refuses every order), testnet.

## What is not built yet

Hyperliquid order signing, the market-data stream, LLM-backed agents, database
schema and the terminal adapter that points OpenCharts at this gateway. The web
container serves the demo terminal until that adapter exists. See
`.superpowers/sdd/ai-platform/plan.md`.
