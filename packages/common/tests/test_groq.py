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
    # additionalProperties is added on the way out; Groq rejects the schema without it.
    assert captured["response_format"]["json_schema"]["schema"] == {
        "type": "object",
        "additionalProperties": False,
    }


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


async def test_a_schema_gains_additional_properties_false_at_every_depth():
    """Groq's strict mode rejects any object node without it. Adding it here rather than
    in every caller keeps the provider out of shared code."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_completion_body())

    nested = {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "meta": {"type": "object", "properties": {"n": {"type": "integer"}}},
            "rows": {"type": "array", "items": {"type": "object", "properties": {}}},
        },
    }
    await _client(handler).complete("q", schema=nested)

    sent = captured["response_format"]["json_schema"]["schema"]
    assert sent["additionalProperties"] is False
    assert sent["properties"]["meta"]["additionalProperties"] is False
    assert sent["properties"]["rows"]["items"]["additionalProperties"] is False


async def test_the_callers_schema_is_not_mutated():
    """Callers pass module-level constants; mutating one would rewrite it process-wide."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion_body())

    original = {"type": "object", "properties": {"a": {"type": "string"}}}
    await _client(handler).complete("q", schema=original)

    assert "additionalProperties" not in original
