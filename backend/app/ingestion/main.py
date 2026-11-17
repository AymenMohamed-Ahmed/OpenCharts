import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from psycopg_pool import AsyncConnectionPool
from redis.asyncio import Redis

from app.common.config import ingestion_settings
from app.common.logging import configure_logging
from app.common.schemas import Health
from app.ingestion.pipelines.redis_out import QuotePublisher
from app.ingestion.pipelines.store import Store
from app.ingestion.pipelines.writer import BatchWriter
from app.ingestion.service import IngestionService
from app.ingestion.sources.hyperliquid import HyperliquidClient, HyperliquidSource

log = logging.getLogger(__name__)


async def _supervise(service: IngestionService) -> None:
    """Restart the loop after a crash; a config error (ValueError) is fatal."""
    while True:
        try:
            await service.run()
        except asyncio.CancelledError:
            raise
        except (ValueError, RuntimeError):
            log.exception("ingestion cannot run with this configuration")
            return
        except Exception:
            log.exception("ingestion crashed; restarting in 5s")
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = ingestion_settings()
    configure_logging(settings.log_level)
    task: asyncio.Task[None] | None = None
    pool: AsyncConnectionPool | None = None
    redis: Redis | None = None
    client: HyperliquidClient | None = None
    if settings.database_url and settings.redis_url:
        pool = AsyncConnectionPool(
            settings.database_url.get_secret_value(), min_size=1, max_size=4, open=False
        )
        await pool.open()
        redis = Redis.from_url(settings.redis_url.get_secret_value())
        client = HyperliquidClient(settings.hl_api_url)
        store = Store(pool)
        service = IngestionService(
            settings,
            client,
            HyperliquidSource(settings),
            store,
            BatchWriter(store),
            QuotePublisher(redis),
        )
        task = asyncio.create_task(_supervise(service))
    else:
        log.warning("DATABASE_URL or REDIS_URL not set; ingestion loop is off")
    yield
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    if client:
        await client.aclose()
    if redis:
        await redis.aclose()
    if pool:
        await pool.close()


app = FastAPI(title="OpenBroker ingestion", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> Health:
    return Health(service="ingestion")
