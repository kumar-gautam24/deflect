"""Provider-agnostic completion interface.

Clients take their credentials explicitly rather than reading a settings singleton:
this package is shared by two services, and a library that reaches into one service's
configuration cannot be used by the other.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    model: str


class LLMClient(Protocol):
    async def complete(self, prompt: str, schema: dict | None = None) -> Completion:
        """Return a completion, constrained to `schema` when the provider supports it."""
        ...


def get_client(
    provider: str,
    model: str,
    api_key: str = "",
    base_url: str = "http://localhost:11434",
) -> LLMClient:
    from deflect_common.llm.gemini import GeminiClient
    from deflect_common.llm.groq import GroqClient
    from deflect_common.llm.ollama import OllamaClient

    if provider == "gemini":
        return GeminiClient(model, api_key)
    if provider == "groq":
        return GroqClient(model, api_key)
    if provider == "ollama":
        return OllamaClient(model, base_url)
    raise ValueError(f"unknown provider: {provider}")
