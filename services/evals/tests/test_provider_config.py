import pytest

from evals.config import get_settings
from evals.main import _make_judge


def test_the_service_refuses_to_build_a_judge_without_a_provider_key(monkeypatch):
    """The design leans on a misconfigured deploy failing at startup rather than serving
    broken answers, and nothing else covers it: the suites override build_judge with a
    fake and ASGITransport never runs the lifespan."""
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="groq api key"):
            _make_judge()
    finally:
        get_settings.cache_clear()


def test_the_service_refuses_a_model_it_cannot_constrain(monkeypatch):
    """Only the gpt-oss family accepts json_schema, and the judge path cannot work
    without it, so this is a startup failure rather than a bad answer later."""
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setenv("JUDGE_MODEL", "llama-3.3-70b-versatile")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="llama-3.3-70b-versatile"):
            _make_judge()
    finally:
        get_settings.cache_clear()


def test_a_correctly_configured_service_builds_its_judge(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    get_settings.cache_clear()
    try:
        assert _make_judge() is not None
    finally:
        get_settings.cache_clear()
