from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.common.config import ingestion_settings
from app.common.logging import configure_logging
from app.common.schemas import Health


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(ingestion_settings().log_level)
    yield


app = FastAPI(title="OpenBroker ingestion", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> Health:
    return Health(service="ingestion")
