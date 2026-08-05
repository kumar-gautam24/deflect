# Groq Provider and Production Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Deflect runnable on a free Groq key, and harden the three services for production without weakening the abuse control merged on 2026-08-05.

**Architecture:** A Groq client in `packages/common` talking to the OpenAI-compatible REST endpoint over `httpx`, with a schema-capability allowlist and 429 backoff enforced at construction and call time respectively. Hardening splits liveness from readiness without cascading, threads a correlation id through every service hop, exposes guarded metrics, and adds gunicorn workers only where they do not break the in-process rate limiter.

**Tech Stack:** Python 3.12, FastAPI, httpx, gunicorn + `uvicorn.workers.UvicornWorker`, prometheus-client, pytest with `asyncio_mode = "auto"`, ruff.

**Spec:** `docs/superpowers/specs/2026-08-05-groq-and-production-hardening-design.md`

## Global Constraints

- Python `>=3.12`. Ruff `line-length = 100`, rules `["E", "F", "I", "UP", "B"]`. Every task ends ruff-clean.
- **No shared tables, no cross-service joins.** No task here creates a migration.
- **`packages/common` receives credentials as arguments, never from a settings singleton** — stated in `llm/base.py`.
- Something two or more services need goes in `packages/common`; something one service needs stays in that service.
- **Commit messages carry no attribution trailers.** Zero exist across the repository's history and their absence is enforced. Lowercase imperative summary, body explaining *why* and what was rejected.
- Comments explain the reasoning and the rejected alternative, not the mechanics.
- **Every test runs with no network and no API key.** A stranger who clones the repo runs the whole suite. Provider calls are faked with `httpx.MockTransport`; clocks and sleeps are injected, never real.
- **Do not weaken the auth policy table.** Every new route gets a principal. `/metrics` is service-only.
- **Never run `docker compose down -v`** — it destroys the volume holding 155 ingested documents.

## File Structure

**Created**

| path | responsibility |
| --- | --- |
| `packages/common/src/deflect_common/llm/groq.py` | Groq client: allowlist, key guard, schema passthrough, 429 backoff. |
| `packages/common/tests/test_groq.py` | All of the above, against `MockTransport`. |
| `packages/common/src/deflect_common/logging.py` | JSON formatter and the request-id `contextvar`. |
| `packages/common/tests/test_logging.py` | Formatter emits parseable JSON carrying the id. |
| `packages/common/src/deflect_common/observability.py` | Correlation-id middleware and the metrics registry/handler. |

**Modified**

| path | change |
| --- | --- |
| `services/{answer,evals}/src/*/config.py` | `groq_api_key`, `provider_api_key`, new model defaults. |
| `services/*/src/*/config.py` | `env`. |
| `services/*/src/*/main.py` | `/ready`, `/metrics`, middleware, docs disabled in production. |
| `services/answer/src/answer/telemetry.py` | Groq rows in `PRICING`. |
| `services/answer/src/answer/retrieval_client.py`, `services/evals/src/evals/answer_client.py` | Forward `X-Request-ID`. |
| `services/*/Dockerfile` | gunicorn, workers, non-root, pinned digest. |
| `docker-compose.yml`, `render.yaml`, `.env.example`, `.github/workflows/*.yml`, `README.md` | Configuration, CI gate move, documentation. |

---

## Task 1: The Groq client

**Files:**
- Create: `packages/common/src/deflect_common/llm/groq.py`
- Test: `packages/common/tests/test_groq.py`
- Modify: `packages/common/src/deflect_common/llm/base.py` (add the `groq` branch to `get_client`)

**Interfaces:**
- Consumes: `Completion` from `deflect_common.llm.base`.
- Produces: `GroqClient(model: str, api_key: str, sleep=asyncio.sleep, transport: httpx.AsyncBaseTransport | None = None)`; `SCHEMA_CAPABLE: frozenset[str]`; `get_client(provider="groq", ...)` returning it.

- [ ] **Step 1: Write the failing tests**

Create `packages/common/tests/test_groq.py`:

```python
import json

import httpx
import pytest
from deflect_common.llm.groq import SCHEMA_CAPABLE, GroqClient

MODEL = "openai/gpt-oss-20b"


def _completion_body(content: str = '{"ok": true}') -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 214, "completion_tokens": 188},
    }


def _client(handler, sleeps: list | None = None) -> GroqClient:
    async def record(seconds: float) -> None:
        (sleeps if sleeps is not None else []).append(seconds)

    return GroqClient(
        MODEL, "test-key", sleep=record, transport=httpx.MockTransport(handler)
    )


async def test_an_empty_api_key_refuses_to_build_a_client():
    with pytest.raises(ValueError, match="api key"):
        GroqClient(MODEL, "")


async def test_a_model_without_schema_support_refuses_to_build_a_client():
    """The answer service cannot work without constrained JSON, so a model that
    cannot produce it is a misconfiguration, not a runtime surprise."""
    with pytest.raises(ValueError, match="llama-3.3-70b-versatile"):
        GroqClient("llama-3.3-70b-versatile", "test-key")


async def test_every_allowlisted_model_builds():
    for model in SCHEMA_CAPABLE:
        assert GroqClient(model, "test-key") is not None


async def test_a_schema_is_sent_as_a_strict_json_schema():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_completion_body())

    await _client(handler).complete("q", schema={"type": "object"})

    assert captured["response_format"]["type"] == "json_schema"
    assert captured["response_format"]["json_schema"]["strict"] is True
    assert captured["response_format"]["json_schema"]["schema"] == {"type": "object"}


async def test_no_response_format_is_sent_without_a_schema():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_completion_body())

    await _client(handler).complete("q")

    assert "response_format" not in captured


async def test_the_api_key_travels_as_a_bearer_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_completion_body())

    await _client(handler).complete("q")

    assert seen["auth"] == "Bearer test-key"


async def test_token_counts_map_onto_the_completion():
    """Groq names these prompt_tokens/completion_tokens where Gemini uses
    prompt_token_count/candidates_token_count. Getting it wrong silently zeroes cost."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion_body("answer text"))

    result = await _client(handler).complete("q")

    assert result.text == "answer text"
    assert result.input_tokens == 214
    assert result.output_tokens == 188
    assert result.model == MODEL


async def test_a_429_is_retried_and_then_succeeds():
    """The free tier allows 8,000 tokens a minute, so a real eval run is throttled.
    Dying on the first 429 would abandon a run partway and leave a partial row."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "3"})
        return httpx.Response(200, json=_completion_body())

    sleeps: list[float] = []
    result = await _client(handler, sleeps).complete("q")

    assert calls["n"] == 2
    assert sleeps == [3.0]
    assert result.input_tokens == 214


async def test_a_missing_retry_after_still_waits():
    """Retrying immediately would just burn another attempt against the same limit."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429) if calls["n"] == 1 else httpx.Response(
            200, json=_completion_body()
        )

    sleeps: list[float] = []
    await _client(handler, sleeps).complete("q")

    assert sleeps and sleeps[0] > 0


async def test_a_sustained_429_eventually_raises():
    """A bounded retry means an outage terminates rather than looping forever."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "0"})

    with pytest.raises(httpx.HTTPStatusError):
        await _client(handler).complete("q")


async def test_a_non_429_error_is_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    with pytest.raises(httpx.HTTPStatusError):
        await _client(handler).complete("q")

    assert calls["n"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/common && uv run pytest tests/test_groq.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'deflect_common.llm.groq'`

- [ ] **Step 3: Write groq.py**

Create `packages/common/src/deflect_common/llm/groq.py`:

```python
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
```

- [ ] **Step 4: Add the provider branch**

In `packages/common/src/deflect_common/llm/base.py`, inside `get_client`, add the import and branch beside the existing two:

```python
    from deflect_common.llm.groq import GroqClient
```
```python
    if provider == "groq":
        return GroqClient(model, api_key)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd packages/common && uv run pytest -q && uv run ruff check .`
Expected: PASS — 35 passed (24 existing plus 11 new). Ruff clean.

- [ ] **Step 6: Commit**

```bash
git add packages/common/src/deflect_common/llm/groq.py packages/common/tests/test_groq.py \
        packages/common/src/deflect_common/llm/base.py
git commit -m "add a groq client that refuses models it cannot constrain

The answer service depends on schema-constrained JSON, and only the
gpt-oss family accepts response_format json_schema -- llama-3.3-70b,
llama-3.1-8b and qwen3.6 reject it outright. A model outside that set is
a misconfiguration, so the client refuses to build rather than failing on
the first real request.

The free tier allows 8,000 tokens a minute, so a real eval run is
throttled. A 429 is retried honouring Retry-After, bounded so a genuine
outage still terminates. The sleep is injected; the tests never wait."
```

---

## Task 2: Wire Groq into the two services

**Files:**
- Modify: `services/answer/src/answer/config.py`, `services/evals/src/evals/config.py`
- Modify: `services/answer/src/answer/main.py`, `services/evals/src/evals/main.py`
- Modify: `services/answer/src/answer/telemetry.py`
- Test: `services/answer/tests/test_provider_config.py` (create)

**Interfaces:**
- Consumes: `GroqClient` and the `groq` branch from Task 1.
- Produces: `Settings.provider_api_key` on both services; `PRICING` entries for both Groq models.

- [ ] **Step 1: Write the failing test**

Create `services/answer/tests/test_provider_config.py`:

```python
from answer.config import Settings
from answer.telemetry import PRICING, estimate_cost


def test_the_key_follows_the_configured_provider():
    """Passing gemini_api_key regardless of provider sends an empty credential the
    moment LLM_PROVIDER is anything else -- a 401 that looks like an outage."""
    groq = Settings(llm_provider="groq", groq_api_key="g", gemini_api_key="x")
    gemini = Settings(llm_provider="gemini", groq_api_key="g", gemini_api_key="x")

    assert groq.provider_api_key == "g"
    assert gemini.provider_api_key == "x"


def test_an_unknown_provider_yields_no_key():
    assert Settings(llm_provider="nonesuch", groq_api_key="g").provider_api_key == ""


def test_both_groq_models_are_priced():
    """estimate_cost returns 0.0 for an unpriced model, which would make the traces
    surface claim every answer was free."""
    for model in ("openai/gpt-oss-120b", "openai/gpt-oss-20b"):
        assert model in PRICING
        assert estimate_cost(model, 1_000_000, 1_000_000) > 0


def test_the_default_provider_is_groq():
    assert Settings().llm_provider == "groq"
    assert Settings().generation_model == "openai/gpt-oss-20b"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd services/answer && DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_answer_test" GEMINI_API_KEY=x uv run pytest tests/test_provider_config.py -q`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'provider_api_key'`

- [ ] **Step 3: Extend both Settings**

In `services/answer/src/answer/config.py`, change the provider block to:

```python
    llm_provider: str = "groq"
    # gpt-oss-20b generates, gpt-oss-120b judges. A judge no stronger than the generator
    # rates its own phrasing highly, and the eval numbers stop meaning anything.
    generation_model: str = "openai/gpt-oss-20b"
    gemini_api_key: str = ""
    groq_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"

    @property
    def provider_api_key(self) -> str:
        """The credential for the configured provider. Passing gemini_api_key whatever
        the provider silently sends an empty key the moment LLM_PROVIDER changes."""
        return {"gemini": self.gemini_api_key, "groq": self.groq_api_key}.get(
            self.llm_provider, ""
        )
```

Apply the same block to `services/evals/src/evals/config.py`, except `judge_model: str = "openai/gpt-oss-120b"` replaces the `generation_model` line.

- [ ] **Step 4: Use it at both construction sites**

In `services/answer/src/answer/main.py`, inside `_make_client`, replace `api_key=settings.gemini_api_key` with `api_key=settings.provider_api_key`. Make the identical change in `services/evals/src/evals/main.py`'s `_make_judge`.

- [ ] **Step 5: Price both models**

In `services/answer/src/answer/telemetry.py`, add to `PRICING`:

```python
    # Groq's paid per-million rates. Not zeroes: the free tier costs nothing today, but
    # a 0.0 here would make every trace claim the answer was free.
    "openai/gpt-oss-120b": (0.15, 0.75),
    "openai/gpt-oss-20b": (0.10, 0.50),
```

- [ ] **Step 6: Run both suites**

```bash
cd services/answer && DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_answer_test" GEMINI_API_KEY=x GROQ_API_KEY=y uv run pytest -q && uv run ruff check .
cd ../evals && DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_evals_test" GEMINI_API_KEY=x GROQ_API_KEY=y uv run pytest -q && uv run ruff check .
```
Expected: answer 47 passed (43 plus 4 new), evals 43 passed 1 skipped. Ruff clean.

Existing conftests set `GEMINI_API_KEY`; if a suite now fails to construct a client because `LLM_PROVIDER` defaults to `groq` with no `GROQ_API_KEY`, set `GROQ_API_KEY` in that conftest beside the token assignments and report it as a deviation.

- [ ] **Step 7: Commit**

```bash
git add services/answer/src/answer/config.py services/evals/src/evals/config.py \
        services/answer/src/answer/main.py services/evals/src/evals/main.py \
        services/answer/src/answer/telemetry.py services/answer/tests/test_provider_config.py
git commit -m "select the provider credential instead of always sending gemini's

Both services passed gemini_api_key whatever LLM_PROVIDER said, so
switching provider sent an empty credential and produced a 401 that
reads as an outage rather than a misconfiguration.

Groq becomes the default so the stack runs on a free key. gpt-oss-20b
generates and gpt-oss-120b judges, keeping the judge the stronger model:
a judge no stronger than the generator rates its own phrasing highly."
```

---

## Task 3: Split liveness from readiness

**Files:**
- Modify: `services/{retrieval,answer,evals}/src/*/main.py`
- Test: `services/{retrieval,answer,evals}/tests/test_health.py` (create in each)

**Interfaces:**
- Consumes: nothing.
- Produces: `GET /health` (liveness, no dependencies) and `GET /ready` (this service's database only) on all three services.

- [ ] **Step 1: Write the failing tests**

Create `services/answer/tests/test_health.py`:

```python
from httpx import ASGITransport, AsyncClient

from answer.main import app


async def get(path: str, headers: dict | None = None):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers or {})


async def test_liveness_touches_no_dependency():
    """A liveness probe that queries the database restarts a healthy process whenever
    Postgres hiccups, which is the opposite of what it is for."""
    response = await get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_reports_the_database():
    response = await get("/ready")

    assert response.status_code == 200
    assert response.json()["database"] == "connected"


async def test_liveness_is_public():
    assert (await get("/health")).status_code != 401


async def test_readiness_is_public():
    assert (await get("/ready")).status_code != 401
```

Create the same file in `services/retrieval/tests/` and `services/evals/tests/`, importing `retrieval.main` / `evals.main` respectively.

Additionally, in `services/answer/tests/test_health.py` only, add the non-cascading test:

```python
async def test_readiness_ignores_an_unreachable_retrieval_service():
    """A readiness check that follows its dependencies turns one service's outage into
    all three reporting unready, so an orchestrator restarts healthy processes and the
    failure amplifies. Retrieval being down is already a 503 on the request that needed
    it."""
    from answer.config import get_settings

    get_settings.cache_clear()
    original = get_settings().retrieval_url
    try:
        get_settings().__dict__["retrieval_url"] = "http://127.0.0.1:1"
        assert (await get("/ready")).status_code == 200
    finally:
        get_settings().__dict__["retrieval_url"] = original
        get_settings.cache_clear()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd services/answer && DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_answer_test" GEMINI_API_KEY=x GROQ_API_KEY=y uv run pytest tests/test_health.py -q`
Expected: FAIL — `/ready` returns 404, and `/health` returns the database field.

- [ ] **Step 3: Split the endpoints**

In each service's `main.py`, replace the existing `health` handler with these two. For `retrieval` use `@app.get`; for `answer` and `evals` use `@router.get`.

```python
@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness: this process is answering. Deliberately touches no dependency -- a
    probe that queries the database restarts a healthy process whenever Postgres
    hiccups, which is the opposite of what liveness is for."""
    return {"status": "ok"}


@app.get("/ready")
async def ready(session: SessionDep) -> dict[str, str]:
    """Readiness: this service can do useful work. Checks only its OWN database.

    It deliberately does not probe the services it calls. A readiness check that
    follows its dependencies turns one outage into all three reporting unready, so an
    orchestrator restarts healthy processes and the failure amplifies instead of
    staying contained.
    """
    await session.execute(text("select 1"))
    return {"status": "ok", "database": "connected"}
```

- [ ] **Step 4: Move the existing assertions**

The three `test_auth_routes.py` files each assert `/health` returns 200 — those still pass. Any existing test asserting `{"status": "ok", "database": "connected"}` from `/health` moves to `/ready`. Search for it: `grep -rn 'database.*connected' services/*/tests/`.

- [ ] **Step 5: Run all three suites**

```bash
for s in retrieval answer evals; do (cd services/$s && DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_${s}_test" GEMINI_API_KEY=x GROQ_API_KEY=y uv run pytest -q && uv run ruff check .); done
```
Expected: retrieval 49, answer 52, evals 47 passed 1 skipped (each gains 4, answer gains 5).

- [ ] **Step 6: Commit**

```bash
git add services/*/src/*/main.py services/*/tests/test_health.py
git commit -m "separate liveness from readiness without cascading

/health queried the database, so it conflated 'this process is answering'
with 'its dependencies are reachable' and would have had an orchestrator
restart healthy processes during a Postgres hiccup.

/ready checks only the service's own database. It deliberately does not
probe the services it calls: a readiness check that follows dependencies
turns one outage into all three reporting unready, amplifying the failure
instead of containing it. Retrieval being down is already a 503 on the
request that needed it."
```

---

## Task 4: Correlation ids and JSON logs

**Files:**
- Create: `packages/common/src/deflect_common/logging.py`, `packages/common/tests/test_logging.py`
- Create: `packages/common/src/deflect_common/observability.py`
- Modify: `services/*/src/*/main.py`, `services/answer/src/answer/retrieval_client.py`, `services/evals/src/evals/answer_client.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `request_id: ContextVar[str]`, `JSONFormatter`, `configure_logging()` in `deflect_common.logging`; `RequestIdMiddleware` in `deflect_common.observability`; both HTTP clients send `X-Request-ID`.

- [ ] **Step 1: Write the failing tests**

Create `packages/common/tests/test_logging.py`:

```python
import json
import logging

from deflect_common.logging import JSONFormatter, request_id


def _format(record_kwargs: dict | None = None) -> dict:
    record = logging.LogRecord(
        name="deflect", level=logging.INFO, pathname="p", lineno=1,
        msg="hello", args=(), exc_info=None,
    )
    for key, value in (record_kwargs or {}).items():
        setattr(record, key, value)
    return json.loads(JSONFormatter().format(record))


def test_a_log_line_is_parseable_json():
    assert _format()["message"] == "hello"


def test_the_line_carries_level_and_logger_name():
    line = _format()
    assert line["level"] == "INFO"
    assert line["logger"] == "deflect"


def test_the_request_id_is_included_when_set():
    """Without this the three-service hop cannot be reassembled from logs at all."""
    token = request_id.set("req-123")
    try:
        assert _format()["request_id"] == "req-123"
    finally:
        request_id.reset(token)


def test_the_field_is_absent_rather_than_null_when_unset():
    assert "request_id" not in _format()


def test_an_exception_is_rendered_into_the_line():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = logging.LogRecord(
            name="d", level=logging.ERROR, pathname="p", lineno=1,
            msg="failed", args=(), exc_info=sys.exc_info(),
        )
        line = json.loads(JSONFormatter().format(record))

    assert "ValueError: boom" in line["exception"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd packages/common && uv run pytest tests/test_logging.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'deflect_common.logging'`

- [ ] **Step 3: Write logging.py**

Create `packages/common/src/deflect_common/logging.py`:

```python
"""Structured logging shared by all three services.

A ~30-line formatter rather than structlog: three services need identical log shape,
which is exactly what this package is for, and one small class is cheaper to own than a
dependency.
"""

import json
import logging
from contextvars import ContextVar

# Request-scoped rather than passed through every call signature. A correlation id
# threaded by hand would have to reach code that has no other reason to know about it.
request_id: ContextVar[str] = ContextVar("request_id", default="")

_BUILTIN = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
}


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        current = request_id.get()
        # Absent rather than null when unset: a null would suggest a request that lost
        # its id, which is a different and more alarming thing than a startup log line.
        if current:
            line["request_id"] = current

        if record.exc_info:
            line["exception"] = self.formatException(record.exc_info)

        line.update(
            {k: v for k, v in record.__dict__.items() if k not in _BUILTIN and k != "message"}
        )
        return json.dumps(line, default=str)


def configure_logging() -> None:
    """Replace the root handler's formatter. Called once at import by each service."""
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
```

- [ ] **Step 4: Write the middleware**

Create `packages/common/src/deflect_common/observability.py`:

```python
"""Request-scoped observability shared by all three services."""

import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from deflect_common.logging import request_id

HEADER = "X-Request-ID"


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Adopt the caller's request id, or mint one.

    Adopting rather than always generating is the whole point: a question entering at the
    web app and reaching retrieval through the answer service carries one id the entire
    way, so three services' logs reassemble into a single story.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        current = request.headers.get(HEADER) or uuid.uuid4().hex
        token = request_id.set(current)
        try:
            response = await call_next(request)
        finally:
            request_id.reset(token)

        # Echoed so a caller can quote it in a bug report.
        response.headers[HEADER] = current
        return response
```

- [ ] **Step 5: Install it in all three services**

In each `main.py`, add near the imports and immediately after the `app = FastAPI(...)` line:

```python
from deflect_common.logging import configure_logging
from deflect_common.observability import RequestIdMiddleware
```
```python
configure_logging()
app.add_middleware(RequestIdMiddleware)
```

- [ ] **Step 6: Forward the id between services**

In `services/answer/src/answer/retrieval_client.py` and `services/evals/src/evals/answer_client.py`, extend the outgoing headers so the id survives the hop:

```python
from deflect_common.logging import request_id
from deflect_common.observability import HEADER
```
```python
                headers = {"Authorization": f"Bearer {self._token}"}
                # Without this the id stops at the service boundary and the trace breaks
                # exactly where a multi-service bug is hardest to follow.
                if request_id.get():
                    headers[HEADER] = request_id.get()
```

Use that `headers` variable in the existing `client.post(...)` call in place of the inline dict.

- [ ] **Step 7: Run everything**

```bash
cd packages/common && uv run pytest -q && uv run ruff check .
for s in retrieval answer evals; do (cd services/$s && DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_${s}_test" GEMINI_API_KEY=x GROQ_API_KEY=y uv run pytest -q && uv run ruff check .); done
```
Expected: common 40 passed (35 plus 5 new); all three service suites unchanged from Task 3.

- [ ] **Step 8: Commit**

```bash
git add packages/common/src/deflect_common/logging.py packages/common/src/deflect_common/observability.py \
        packages/common/tests/test_logging.py services/*/src/*/main.py \
        services/answer/src/answer/retrieval_client.py services/evals/src/evals/answer_client.py
git commit -m "thread one request id through all three services

A question entering at the web app and reaching retrieval through the
answer service now carries a single id the whole way, so three services'
logs reassemble into one story instead of three disconnected ones.

The id is adopted from the caller when present rather than always minted,
which is what makes the hop traceable. A contextvar rather than a
parameter, so code with no other reason to know about correlation does
not have to carry it.

A thirty-line formatter rather than structlog: three services need one
log shape, which is what packages/common is for."
```

---

## Task 5: Guarded metrics, and close the docs gap

**Files:**
- Modify: `packages/common/src/deflect_common/observability.py`
- Modify: `services/*/src/*/config.py` (add `env`), `services/*/src/*/main.py`
- Modify: `packages/common/pyproject.toml`, three service `pyproject.toml` files
- Test: `services/answer/tests/test_metrics.py` (create)

**Interfaces:**
- Consumes: `require_service` from each service's `main.py`; `RequestIdMiddleware` from Task 4.
- Produces: `metrics_response() -> Response` in `deflect_common.observability`; `GET /metrics` guarded by `require_service` on all three services; `Settings.env`.

- [ ] **Step 1: Write the failing test**

Create `services/answer/tests/test_metrics.py`:

```python
from httpx import ASGITransport, AsyncClient

from answer.main import app


async def get(path: str, headers: dict | None = None):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers or {})


async def test_metrics_requires_a_service_credential():
    """Request volumes and latencies are operational intelligence. The policy table is
    'the security policy in full', so a new public endpoint would be exactly the drift
    it exists to prevent."""
    assert (await get("/metrics")).status_code == 401


async def test_metrics_serves_a_prometheus_exposition_to_a_service_caller():
    response = await get("/metrics", {"Authorization": "Bearer test-service-token"})

    assert response.status_code == 200
    assert "python_info" in response.text


async def test_an_operator_credential_does_not_open_metrics():
    response = await get("/metrics", {"Authorization": "Bearer test-operator-token"})

    assert response.status_code == 401
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd services/answer && DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_answer_test" GEMINI_API_KEY=x GROQ_API_KEY=y uv run pytest tests/test_metrics.py -q`
Expected: FAIL — `/metrics` returns 404.

- [ ] **Step 3: Add the dependency**

Add `"prometheus-client>=0.21"` to the `dependencies` list in `packages/common/pyproject.toml`, then run `uv lock` in `packages/common` **and in all three services** — each resolves `deflect-common` through its own lockfile, and CI's plain `uv sync` relocks silently rather than failing.

- [ ] **Step 4: Add the metrics handler**

Append to `packages/common/src/deflect_common/observability.py`:

```python
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest


def metrics_response() -> Response:
    """Prometheus exposition for the default registry.

    Guarded by the service principal at every call site: request volumes and latencies
    are operational intelligence, and every route in this system has a principal.
    """
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

- [ ] **Step 5: Add `env` and mount the route**

In each service's `config.py`, add to `Settings`:

```python
    # Interactive API docs are useful in development and are an inventory of the attack
    # surface in production, where nobody is browsing them anyway.
    env: str = "development"
```

In each `main.py`, change the `FastAPI(...)` construction so production serves no docs, and mount the guarded route:

```python
_settings = get_settings()
_docs = {} if _settings.env != "production" else {
    "docs_url": None, "redoc_url": None, "openapi_url": None
}
app = FastAPI(title="Deflect answer", lifespan=lifespan, **_docs)
```

```python
@app.get("/metrics", dependencies=[Depends(require_service)])
async def metrics() -> Response:
    return metrics_response()
```

For `answer` and `evals` use `@router.get`. Import `Response` from `fastapi` and `metrics_response` from `deflect_common.observability`. Note `retrieval/main.py` and `evals/main.py` may not yet bind `_settings`; reuse the existing `get_settings()` call that builds the guards rather than adding a second one.

**The evals service needs a rename first.** Its service guard is currently called
`_require_service_at_startup`, and its comment says it is "built but never attached to a
route: no evals route has the service principal. It exists so an unset SERVICE_TOKEN aborts
this import." Mounting `/metrics` makes that untrue — the guard now genuinely protects a
route. Rename it to `require_service` and replace the comment with:

```python
# Guards /metrics, and its construction aborts the import when SERVICE_TOKEN is unset, so
# a deploy that forgot the credential refuses to start rather than failing partway through
# an eval run.
require_service = bearer_guard(get_settings().service_token, "service")
```

`retrieval` and `answer` already expose `require_service` and need no rename. Leaving evals'
name and comment as they are would make both a lie, and an underscore-prefixed private name
guarding a public route is exactly the kind of thing a later reader deletes as dead code.

- [ ] **Step 6: Run everything**

```bash
for s in retrieval answer evals; do (cd services/$s && DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_${s}_test" GEMINI_API_KEY=x GROQ_API_KEY=y uv run pytest -q && uv run ruff check .); done
cd packages/common && uv run pytest -q && uv run ruff check .
```
Expected: answer 55 passed (52 plus 3 new); retrieval and evals unchanged.

- [ ] **Step 7: Commit**

```bash
git add packages/common/src/deflect_common/observability.py packages/common/pyproject.toml \
        packages/common/uv.lock services/*/uv.lock services/*/pyproject.toml \
        services/*/src/*/config.py services/*/src/*/main.py services/answer/tests/test_metrics.py
git commit -m "expose metrics behind the service credential

Request volumes and latencies are operational intelligence, and the auth
spec calls its route table the security policy in full -- a new public
endpoint absent from it would be exactly the drift the table prevents.
Scrapers are machines, so a bearer token costs them nothing.

Interactive docs are disabled when env is production, closing a gap the
auth review found: /docs, /redoc and /openapi.json were public on all
three services and falsified the claim that retrieval has no public route
but /health."
```

---

## Task 6: Gunicorn, workers, and container hardening

**Files:**
- Modify: `services/{retrieval,answer,evals}/Dockerfile`
- Modify: three service `pyproject.toml` files

**Interfaces:**
- Consumes: `/ready` from Task 3.
- Produces: containers running gunicorn with the uvicorn worker class, as a non-root user.

**The worker count is the point of this task.** Retrieval and evals run multiple workers; **the answer service runs exactly one**. `_ask_limiter` is an in-process dictionary, so N workers would mean N independent limiters and `ask_rate_limit_per_hour = 20` would silently become 20×N.

- [ ] **Step 1: Add gunicorn to each service**

Add `"gunicorn>=23.0"` to the `dependencies` list in each of `services/{retrieval,answer,evals}/pyproject.toml`, then `uv lock` in each.

- [ ] **Step 2: Rewrite the retrieval Dockerfile tail**

Replace the final `CMD` line of `services/retrieval/Dockerfile` with:

```dockerfile
# Runs as a non-root user: a container process that does not need root should not have
# it, and this costs nothing to arrange.
RUN useradd --system --uid 10001 deflect && chown -R deflect /app
USER deflect

# Two workers because retrieval embeds and reranks in-process, which is CPU-bound and
# blocks the event loop -- async concurrency alone does not help there. Gunicorn also
# brings graceful shutdown, so in-flight requests finish on SIGTERM.
CMD ["gunicorn", "retrieval.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "2", "--bind", "0.0.0.0:8001", \
     "--graceful-timeout", "30", "--timeout", "120"]
```

Apply the same `useradd`/`USER` pair to the other two Dockerfiles.

- [ ] **Step 3: Rewrite the evals Dockerfile tail**

```dockerfile
CMD ["gunicorn", "evals.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "2", "--bind", "0.0.0.0:8003", \
     "--graceful-timeout", "30", "--timeout", "120"]
```

- [ ] **Step 4: Rewrite the answer Dockerfile tail — one worker, with the reason**

```dockerfile
# EXACTLY ONE WORKER, deliberately. The per-address rate limiter in ratelimit.py is an
# in-process dict, so N workers would mean N independent limiters and a 20-per-hour
# limit would silently become 20N. This service awaits a model provider rather than
# burning CPU, so async concurrency already carries its load and workers would buy
# almost nothing anyway.
#
# When the job queue arrives and brings Redis, the limiter moves there and this can
# scale out. Until then, raising this number weakens the abuse control.
CMD ["gunicorn", "answer.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "1", "--bind", "0.0.0.0:8002", \
     "--graceful-timeout", "30", "--timeout", "300"]
```

The longer timeout is for the streaming `/ask` response.

- [ ] **Step 5: Pin the base images**

Resolve the current digest once and use it in all three Dockerfiles:

```bash
docker pull python:3.12-slim
docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim
```

Replace `FROM python:3.12-slim` in each with `FROM python:3.12-slim@sha256:<digest>` using the value printed, and add above it:

```dockerfile
# Pinned by digest: the slim tag moves, so an unpinned base means two builds of the same
# commit can differ.
```

- [ ] **Step 6: Verify the stack still comes up**

```bash
cd /Users/gautam/Downloads/Projects/deflect
docker compose up -d --build
for p in 8001 8002 8003; do curl -fsS localhost:$p/health; echo; done
for p in 8001 8002 8003; do curl -fsS localhost:$p/ready; echo; done
docker compose exec -T answer ps -o user= -p 1
docker compose logs answer --tail 5
```
Expected: three `{"status": "ok"}` from `/health`, three with `"database": "connected"` from `/ready`, `deflect` as the process user, and gunicorn's startup lines in the logs. **Do not run `docker compose down -v`.**

- [ ] **Step 7: Commit**

```bash
git add services/*/Dockerfile services/*/pyproject.toml services/*/uv.lock
git commit -m "run under gunicorn, one worker where the limiter demands it

Retrieval and evals get two workers each: retrieval embeds and reranks
in-process, which is CPU-bound and blocks the event loop, so async
concurrency alone does not help.

The answer service gets exactly one. Its rate limiter is an in-process
dict, so N workers would mean N independent limiters and twenty requests
an hour would silently become twenty N. It awaits a provider rather than
burning CPU, so workers would buy it almost nothing anyway. When the job
queue brings Redis the limiter moves there and this can scale out.

Containers run as a non-root user and pin their base by digest, since the
slim tag moves and two builds of one commit could otherwise differ."
```

---

## Task 7: Configuration, the CI gate, and the README

**Files:**
- Modify: `docker-compose.yml`, `render.yaml`, `.env.example`
- Modify: `.github/workflows/ci.yml`, `.github/workflows/nightly-evals.yml`
- Modify: `README.md`

**Interfaces:**
- Consumes: every setting introduced in Tasks 2–6.
- Produces: a stack that runs on Groq and a CI pipeline whose PR jobs stay fast.

- [ ] **Step 1: Compose**

Add to the `answer` and `evals` environment blocks:

```yaml
      GROQ_API_KEY: ${GROQ_API_KEY:-}
      LLM_PROVIDER: ${LLM_PROVIDER:-groq}
```

Add to all three:

```yaml
      ENV: ${ENV:-development}
```

- [ ] **Step 2: Render**

Add `GROQ_API_KEY` and `ENV` with `sync: false` to `deflect-answer` and `deflect-evals`; add `ENV` to `deflect-retrieval`. Change every `healthCheckPath` from `/health` to `/ready` — Render's check should mean "can serve traffic", which is readiness, not "the process exists".

- [ ] **Step 3: .env.example**

```bash
# Provider. Groq's free tier runs the whole stack; get a key at https://console.groq.com/keys
LLM_PROVIDER=groq
GROQ_API_KEY=
# Only needed if LLM_PROVIDER=gemini.
GEMINI_API_KEY=

# production disables the interactive API docs.
ENV=development
```

Keep the existing token and corpus entries.

- [ ] **Step 4: Move the eval gate off pull requests**

In `.github/workflows/ci.yml`, change the trigger and gate the eval job:

```yaml
on:
  push:
  pull_request:
```

Add to the `eval-smoke` job only:

```yaml
    # Ten items is roughly fourteen minutes against the free tier's 8,000 tokens a
    # minute. Blocking every pull request for that long trains people to ignore the
    # gate, which is worse than running it on main and nightly where it still catches
    # a faithfulness regression before it ships.
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
```

Add `GROQ_API_KEY: ${{ secrets.GROQ_API_KEY }}` and `LLM_PROVIDER: groq` to that job's compose-up environment, and the same to `nightly-evals.yml`. Change both workflows' health-wait loops from `/health` to `/ready`.

- [ ] **Step 5: README**

Add to the policy table two rows: `/ready` — public, on all three; `/metrics` — service, on all three. Add a line noting `/docs`, `/redoc` and `/openapi.json` are public in development and absent when `ENV=production`.

Under the results tables, relabel the generation metrics:

```markdown
The generation metrics below were produced with `openai/gpt-oss-20b` generating and
`openai/gpt-oss-120b` judging, on Groq.

An earlier set of these numbers was produced with `gemini-2.0-flash` generating and
`gemini-2.0-pro` judging. They are kept in git history rather than shown here, because
reproducing them needs a Gemini key this deployment does not have. A side-by-side
comparison was considered and rejected: without one judge scoring both generators, the
generator and the judge both change and the table cannot attribute a difference to
either, which is worse than showing one column honestly.

The retrieval tables above are unaffected — they are deterministic and LLM-free, and were
produced without any provider key.
```

Add a "Running in production" subsection covering gunicorn worker counts and **why the answer service is pinned to one**, the liveness/readiness split, `X-Request-ID`, and the guarded `/metrics`.

- [ ] **Step 6: Re-run the generation metrics**

With a real `GROQ_API_KEY` in `.env` and the stack up:

```bash
curl -X POST localhost:8003/runs -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $OPERATOR_TOKEN" -d '{"limit": null}'
```

This takes roughly 110 minutes at 8,000 tokens per minute. Put the resulting metrics into the README's generation table. If it fails partway, the 429 backoff from Task 1 is the thing to check first.

- [ ] **Step 7: Full verification**

```bash
for s in retrieval answer evals; do (cd services/$s && DATABASE_URL="postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_${s}_test" GEMINI_API_KEY=x GROQ_API_KEY=y uv run pytest -q && uv run ruff check .); done
cd packages/common && uv run pytest -q && uv run ruff check .
cd ../../apps/web && npm test && npm run lint && npm run build
```

Report the real counts and correct the README if they differ from what you write.

- [ ] **Step 8: Commit**

```bash
git add docker-compose.yml render.yaml .env.example .github/workflows/ci.yml \
        .github/workflows/nightly-evals.yml README.md
git commit -m "run on groq, and stop blocking pull requests on a slow gate

Ten eval items is about fourteen minutes against the free tier's 8,000
tokens a minute. A gate that slow on every pull request trains people to
ignore it, so it moves to pushes on main plus the nightly, where it still
catches a faithfulness regression before it ships.

Render's health check moves to /ready: it decides whether a deploy can
serve traffic, which is readiness, not whether the process exists.

The generation numbers are relabelled rather than compared. Without one
judge scoring both generators the table could not attribute a difference
to either, which is worse than one honest column."
```

---

## Self-Review

**Spec coverage.** Groq client, allowlist and key guard → Task 1. Model split, `provider_api_key`, `PRICING` → Task 2. 429 backoff → Task 1. Liveness/readiness and non-cascading → Task 3. Correlation ids and JSON logs → Task 4. Guarded `/metrics` and the docs gap → Task 5. Gunicorn, the worker/limiter collision, non-root, pinned digests → Task 6. Compose, Render, CI gate move, README relabelling → Task 7. Reasoning tokens are explicitly out of scope in the spec and correctly absent here.

**Type consistency.** `GroqClient(model, api_key, sleep, transport)` is constructed in Task 1's tests exactly as declared and reached through `get_client` in Task 2. `Settings.provider_api_key` is a property in Task 2 and read in the same task's `_make_client`. `request_id` and `HEADER` from Task 4 are imported by both HTTP clients in the same task. `metrics_response()` from Task 5 is called by all three services in that task. `require_service` exists in `retrieval` and `answer` already; Task 5 renames evals' `_require_service_at_startup` to match, because mounting `/metrics` makes it guard a route rather than only abort an import.

**Known gaps, recorded rather than hidden:**

- Expected test counts assume each new test passes; Tasks 3 and 5 add files to three services at once, so a per-service count may differ. Use what the suite reports and say so.
- Task 3's non-cascading test mutates a cached `Settings` through `__dict__`, which is blunt. It is the cheapest way to point the answer service at a dead address without a fixture rewrite; if it proves brittle, a monkeypatched `build_retrieval` is the fallback.
- Task 6 verifies gunicorn by container smoke test only. Worker supervision is not observable in-process, and asserting on worker count from inside a worker would test the wrong thing.
- Task 7 Step 6 needs a live key and about two hours. It is the only step in this plan that cannot run offline.
