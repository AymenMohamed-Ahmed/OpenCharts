from app.agents.base import Agent, MarketContext
from app.common.schemas import OrderIntent


class FlatAgent(Agent):
    """Never trades. Placeholder so the registry has one entry."""

    name = "flat"

    async def propose(self, context: MarketContext) -> list[OrderIntent]:
        return []
