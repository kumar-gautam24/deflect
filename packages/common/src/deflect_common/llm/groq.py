"""Groq provider.

Talks to the OpenAI-compatible REST endpoint through httpx rather than the Groq SDK.
httpx is already a dependency, ollama.py sets the precedent for reaching a provider over
plain HTTP, and an SDK added for one POST is a dependency bought for nothing.
"""

import asyncio
from collections.abc import Awaitable, Callable

import httpx

from deflect_common.llm.base import Completion

BASE_URL = "https://api.groq.com/openai/v1"

# Only these models accept response_format: json_schema. The answer service depends on
# constrained output for {answer, cited_chunk_ids, grounded}, so a model outside this set
# cannot serve it at all -- llama-3.3-70b, llama-3.1-8b and qwen3.6 reject the parameter
# outright. Verified against the live API on 2026-08-05; the good Llama models are
# unavailable to this system regardless of their quality.
SCHEMA_CAPABLE = frozenset({"openai/gpt-oss-120b", "openai/gpt-oss-20b"})

# The free tier allows 8,000 tokens per minute, so a real eval run WILL be throttled.
# Retrying is the difference between a run that pauses and one that dies at item 47,
# wasting 45 minutes and leaving a partial EvalRun row behind.
MAX_ATTEMPTS = 5
FALLBACK_RETRY_SECONDS = 20.0


def _retry_after(response: httpx.Response) -> float:
    """Seconds to wait before retrying a 429.

    A missing or unparseable header falls back to a fixed delay rather than retrying at
    once, which would spend another attempt against the same limit for nothing.
    """
    try:
        return max(float(response.headers.get("retry-after", "")), 0.0)
    except ValueError:
        return FALLBACK_RETRY_SECONDS


class GroqClient:
    def __init__(
        self,
        model: str,
        api_key: str,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Both checks raise here rather than on the first request, so a misconfigured
        deploy refuses traffic instead of taking it and producing a broken answer. Groq
        answers 400 for an unsupported model, which would otherwise look like an outage.

        `sleep` and `transport` are injected so the backoff tests neither wait nor reach
        the network.
        """
        if not api_key:
            raise ValueError("the groq api key is empty; refusing to build a client")
        if model not in SCHEMA_CAPABLE:
            raise ValueError(
                f"{model} cannot produce schema-constrained output; "
                f"choose one of {sorted(SCHEMA_CAPABLE)}"
            )

        self._model = model
        self._api_key = api_key
        self._sleep = sleep
        self._transport = transport

    async def complete(self, prompt: str, schema: dict | None = None) -> Completion:
        payload: dict = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            # Temperature zero for the same reason as the other providers: both callers
            # are graded, so sampling noise would surface as run-to-run metric drift
            # with no code change behind it.
            "temperature": 0.0,
        }
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "strict": True, "schema": schema},
            }

        body = await self._post(payload)
        usage = body["usage"]
        # completion_tokens counts reasoning tokens too, which is correct for cost:
        # Groq bills all of them, and on these models most output is reasoning.
        return Completion(
            text=body["choices"][0]["message"]["content"],
            input_tokens=usage["prompt_tokens"],
            output_tokens=usage["completion_tokens"],
            model=self._model,
        )

    async def _post(self, payload: dict) -> dict:
        url = f"{BASE_URL}/chat/completions"
        headers = {"Authorization": f"Bearer {self._api_key}"}

        async with httpx.AsyncClient(timeout=120, transport=self._transport) as client:
            for _ in range(MAX_ATTEMPTS - 1):
                response = await client.post(url, json=payload, headers=headers)
                if response.status_code != 429:
                    response.raise_for_status()
                    return response.json()
                await self._sleep(_retry_after(response))

            # The final attempt is outside the loop so a 429 here raises like any other
            # error. A sustained outage terminates instead of retrying forever.
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()
