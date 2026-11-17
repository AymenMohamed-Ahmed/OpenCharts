from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.agents.base import Agent
from app.agents.strategies.flat import FlatAgent
from app.common.config import agent_settings
from app.common.logging import configure_logging
from app.common.schemas import Health

REGISTRY: dict[str, Agent] = {agent.name: agent for agent in (FlatAgent(),)}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(agent_settings().log_level)
    yield


app = FastAPI(title="OpenBroker agents", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> Health:
    return Health(service="agents")


@app.get("/internal/agents")
def list_agents() -> list[str]:
    return sorted(REGISTRY)
