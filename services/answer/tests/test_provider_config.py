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
