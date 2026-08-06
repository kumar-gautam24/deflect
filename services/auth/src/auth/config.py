from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://deflect:deflect@localhost:5432/deflect_auth"
    redis_url: str = "redis://localhost:6379/0"

    service_token: str = ""
    operator_token: str = ""

    # production disables the interactive API docs.
    env: str = "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()
