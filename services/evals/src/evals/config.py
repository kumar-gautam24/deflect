from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_evals"
    answer_url: str = "http://localhost:8002"

    # Both are read at runtime and must work inside a container, where there is no
    # git binary and no repository checkout. The defaults resolve against the source
    # tree for local runs; the image sets them explicitly.
    dataset_path: Path = Path(__file__).parents[4] / "evals" / "golden.yaml"
    git_sha: str = ""

    llm_provider: str = "gemini"
    judge_model: str = "gemini-2.0-pro"
    gemini_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434"


@lru_cache
def get_settings() -> Settings:
    return Settings()
