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

    llm_provider: str = "gemini"
    generation_model: str = "gemini-2.0-flash"
    gemini_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"

    # Cross-encoder logits, not similarities. Chosen from the swept curve: answers
    # 83 percent of answerable questions, passing 13 percent of unanswerable ones to
    # the grounding check.
    min_top_score: float = 2.0
    min_margin: float = 0.0

    web_origin: str = "http://localhost:3000"


@lru_cache
def get_settings() -> Settings:
    return Settings()
