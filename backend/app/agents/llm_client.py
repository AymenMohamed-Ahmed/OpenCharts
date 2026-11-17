"""LLM access for agents.

Only the interface is defined here. The provider-backed client is part of the
agents bundle in the plan, so no SDK is a dependency yet.
"""

from typing import Protocol


class LLMClient(Protocol):
    async def complete(self, system: str, prompt: str) -> str: ...
