from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = (
        "postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_retrieval"
    )
    # Empty by default so a deployment that forgets them fails at import rather than
    # serving open routes. docker-compose supplies development values.
    service_token: str = ""
    operator_token: str = ""
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    # Bounds peak memory during ingest. One document in the corpus chunks into 1,066
    # pieces, and embedding them in a single call allocated 7.4 GB and was OOM-killed
    # in a container; at 32 the same file peaks at 1.4 GB.
    embedding_batch_size: int = 32
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"


@lru_cache
def get_settings() -> Settings:
    return Settings()
