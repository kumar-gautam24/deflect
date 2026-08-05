from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_answer"
    retrieval_url: str = "http://localhost:8001"

    # Empty by default so a deployment that forgets them fails at import rather than
    # serving open routes. docker-compose supplies development values.
    service_token: str = ""
    operator_token: str = ""

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

    # Cross-encoder logits, not similarities. Chosen from the swept curve: answers
    # 83 percent of answerable questions, passing 13 percent of unanswerable ones to
    # the grounding check.
    min_top_score: float = 2.0
    min_margin: float = 0.0

    web_origin: str = "http://localhost:3000"

    # Derived together, not independently. At the gemini-2.0-flash prices in
    # telemetry.py a five-chunk question costs about $0.00055, so 500 a day caps a
    # fully abused day near $0.28 -- roughly $8.50 a month sustained, an order of
    # magnitude above real demo traffic.
    #
    # 20 an hour over 24 hours is 480, just under the daily ceiling, so no single
    # address can exhaust the budget in a day. Raising the hourly limit past 21 breaks
    # that property; re-derive both together if either changes.
    ask_rate_limit_per_hour: int = 20
    ask_daily_limit: int = 500


@lru_cache
def get_settings() -> Settings:
    return Settings()
