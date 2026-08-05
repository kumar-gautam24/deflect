"""Groq provider.

Talks to the OpenAI-compatible REST endpoint through httpx rather than the Groq SDK.
httpx is already a dependency, ollama.py sets the precedent for reaching a provider over
plain HTTP, and an SDK added for one POST is a dependency bought for nothing.
"""

import asyncio
import math
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
        seconds = float(response.headers.get("retry-after", ""))
    except ValueError:
        return FALLBACK_RETRY_SECONDS

    # inf would sleep forever and nan raises from asyncio.sleep, so a malformed header
    # would turn a bounded retry into a hang or a crash.
    if not math.isfinite(seconds):
        return FALLBACK_RETRY_SECONDS

    return max(seconds, 0.0)


def _raise_for_status(response: httpx.Response) -> None:
    """Raise on an error status, with the provider's explanation attached.

    httpx's own message carries only the status line, and Groq puts the actionable part --
    which schema key it rejected, which model lacks a capability -- in the body. A bare
    "400 Bad Request" in a log costs an hour that the body would have saved.
    """
    if response.is_success:
        return

    detail = response.text[:500]
    raise httpx.HTTPStatusError(
        f"{response.status_code} from groq: {detail}", request=response.request, response=response
    )


def _strict(schema: dict) -> dict:
    """Return `schema` with additionalProperties: false on every object node.

    Groq's strict mode rejects a schema without it, on every object, at any depth. Applied
    here rather than asked of callers: Gemini and Ollama need no such key, so making every
    caller carry a Groq-specific field would leak this provider into the shared code that
    exists precisely to hide it.

    Builds new dicts rather than mutating, because callers pass module-level constants and
    a mutation would quietly rewrite one for the whole process.
    """
    if schema.get("type") == "object":
        schema = schema | {"additionalProperties": False}
        properties = schema.get("properties")
        if properties:
            schema = schema | {"properties": {k: _strict(v) for k, v in properties.items()}}

    items = schema.get("items")
    if isinstance(items, dict):
        schema = schema | {"items": _strict(items)}

    return schema


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
                "json_schema": {"name": "response", "strict": True, "schema": _strict(schema)},
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
                    _raise_for_status(response)
                    return response.json()
                await self._sleep(_retry_after(response))

            # The final attempt is outside the loop so a 429 here raises like any other
            # error. A sustained outage terminates instead of retrying forever.
            response = await client.post(url, json=payload, headers=headers)
            _raise_for_status(response)
            return response.json()
