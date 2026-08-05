import pytest

from answer.config import Settings, get_settings
from answer.main import _make_client
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


def test_the_service_refuses_to_build_a_client_without_a_provider_key(monkeypatch):
    """The design leans on a misconfigured deploy failing at startup rather than serving
    broken answers, and nothing else covers it: the suites override build_client with a
    fake and ASGITransport never runs the lifespan."""
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="groq api key"):
            _make_client()
    finally:
        get_settings.cache_clear()


def test_the_service_refuses_a_model_it_cannot_constrain(monkeypatch):
    """Only the gpt-oss family accepts json_schema, and the answer path cannot work
    without it, so this is a startup failure rather than a bad answer later."""
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setenv("GENERATION_MODEL", "llama-3.3-70b-versatile")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="llama-3.3-70b-versatile"):
            _make_client()
    finally:
        get_settings.cache_clear()


def test_a_correctly_configured_service_builds_its_client(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    get_settings.cache_clear()
    try:
        assert _make_client() is not None
    finally:
        get_settings.cache_clear()
