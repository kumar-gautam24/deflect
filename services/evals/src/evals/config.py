from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_evals"
    answer_url: str = "http://localhost:8002"

    llm_provider: str = "gemini"
    judge_model: str = "gemini-2.0-pro"
    gemini_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"


@lru_cache
def get_settings() -> Settings:
    return Settings()
