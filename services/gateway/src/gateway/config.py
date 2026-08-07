from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    retrieval_url: str = "http://localhost:8001"
    answer_url: str = "http://localhost:8002"
    evals_url: str = "http://localhost:8003"
    auth_url: str = "http://localhost:8004"

    redis_url: str = "redis://localhost:6379/0"

    service_token: str = ""
    operator_token: str = ""

    # Settings rather than a constant: this is the number an operator turns down when the
    # provider bill starts climbing, and needing a deploy to do that defeats the point.
    ask_rate_limit_per_hour: int = 20

    # How many proxies sit in front of this process. One on Render; zero locally, where
    # the fallback to the peer address is the right answer anyway.
    trusted_proxy_hops: int = 1

    # production disables the interactive API docs.
    env: str = "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()
