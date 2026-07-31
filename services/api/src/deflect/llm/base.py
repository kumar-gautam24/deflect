"""Provider-agnostic completion interface."""

from dataclasses import dataclass
from typing import Annotated, Protocol

from fastapi import Depends


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


def get_client(provider: str | None = None, model: str | None = None) -> LLMClient:
    # Imported here rather than at module scope so importing the protocol does not
    # pull in both provider SDKs.
    from deflect.config import get_settings
    from deflect.llm.gemini import GeminiClient
    from deflect.llm.ollama import OllamaClient

    settings = get_settings()
    provider = provider or settings.llm_provider
    model = model or settings.generation_model

    if provider == "gemini":
        return GeminiClient(model)
    if provider == "ollama":
        return OllamaClient(model)
    raise ValueError(f"unknown provider: {provider}")


# Mirrors SessionDep in deflect.db: routes declare the dependency by type, which is
# what keeps Depends() out of argument defaults.
ClientDep = Annotated[LLMClient, Depends(get_client)]
