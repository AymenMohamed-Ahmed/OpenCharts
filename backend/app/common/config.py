"""Per-service settings.

Each service has its own settings class and reads only the variables it needs.
docker-compose.yml passes each container only its own secrets, so the agents
container never holds broker keys and the execution container never holds LLM
keys. tests/test_compose_isolation.py enforces that split.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

HL_API_URLS = {
    "mainnet": "https://api.hyperliquid.xyz",
    "testnet": "https://api.hyperliquid-testnet.xyz",
}


def _csv(value: str, *, upper: bool = True) -> list[str]:
    items = [i.strip() for i in value.split(",") if i.strip()]
    return list(dict.fromkeys(i.upper() if upper else i for i in items))


class _Base(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    log_level: str = "INFO"


class ApiSettings(_Base):
    """Gateway and risk gate."""

    api_auth_token: SecretStr
    execution_internal_token: SecretStr
    execution_url: str = "http://execution:8000"
    agents_url: str = "http://agents:8000"

    trading_enabled: bool = False
    max_order_notional_usd: float = 100.0
    allowed_symbols: str = ""

    database_url: SecretStr | None = None
    redis_url: SecretStr | None = None

    @property
    def allowed_symbol_set(self) -> frozenset[str]:
        return frozenset(s.strip().upper() for s in self.allowed_symbols.split(",") if s.strip())


class AgentSettings(_Base):
    """LLM agents. No broker or database credentials."""

    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    llm_model: str = "claude-fable-5-2"


class IngestionSettings(_Base):
    """Market-data pipelines. Hyperliquid market data is public, so no key."""

    hl_network: Literal["mainnet", "testnet"] = "testnet"
    database_url: SecretStr | None = None
    redis_url: SecretStr | None = None

    ingest_coins: str = "BTC,ETH"
    ingest_intervals: str = "1m,1h"
    ingest_backfill_days: int = Field(default=7, ge=1, le=365)
    quote_retention_days: int = Field(default=7, ge=1, le=3650)

    @property
    def hl_api_url(self) -> str:
        return HL_API_URLS[self.hl_network]

    @property
    def coins(self) -> list[str]:
        return _csv(self.ingest_coins)

    @property
    def intervals(self) -> list[str]:
        return _csv(self.ingest_intervals, upper=False)


class MigrateSettings(_Base):
    """The one-shot migrate service: database access only."""

    database_url: SecretStr


class ExecutionSettings(_Base):
    """Broker execution. The only service that holds broker credentials."""

    execution_internal_token: SecretStr
    broker: Literal["paper", "hyperliquid"] = "paper"
    hl_network: Literal["mainnet", "testnet"] = "testnet"
    hl_account_address: str = ""
    hl_agent_private_key: SecretStr | None = None
    live_trading: bool = False

    @property
    def hl_api_url(self) -> str:
        return HL_API_URLS[self.hl_network]

    @model_validator(mode="after")
    def _guard_live_trading(self) -> "ExecutionSettings":
        if self.broker == "hyperliquid":
            if not self.hl_account_address or self.hl_agent_private_key is None:
                raise ValueError(
                    "BROKER=hyperliquid needs HL_ACCOUNT_ADDRESS and HL_AGENT_PRIVATE_KEY"
                )
            if self.hl_network == "mainnet" and not self.live_trading:
                raise ValueError("Hyperliquid mainnet needs LIVE_TRADING=true")
        return self


@lru_cache
def api_settings() -> ApiSettings:
    return ApiSettings()


@lru_cache
def agent_settings() -> AgentSettings:
    return AgentSettings()


@lru_cache
def ingestion_settings() -> IngestionSettings:
    return IngestionSettings()


@lru_cache
def execution_settings() -> ExecutionSettings:
    return ExecutionSettings()
