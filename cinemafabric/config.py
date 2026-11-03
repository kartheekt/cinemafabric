"""Runtime settings from the environment and .env."""

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="CF_", extra="ignore")

    mode: str = "live"
    profile: str = ""
    meta_api_key: str = Field(default="", validation_alias=AliasChoices("MODEL_API_KEY", "CF_META_API_KEY"))
    meta_api_base: str = "https://api.meta.ai/v1"
    join_token: str = ""
    fabric_token: str = ""
    admin_password: str = ""
    database_url: str = ""
    media_bucket: str = ""
    c2pa_cert: str = ""
    c2pa_key: str = ""
    s3_endpoint: str = ""
    parallel_productions: int = 1
    accounts: bool = False
    signup: str = "invite"
    user_daily_usd: float = 2.0
    user_max_active: int = 2
    fabric_url: str = "http://127.0.0.1:8100"
    data_dir: Path = Path("data")
    health_interval_s: float = 5.0
    cloud_concurrency: int = 8

    @property
    def mock(self) -> bool:
        return self.mode.lower() == "mock"

    def path(self, *parts: str) -> Path:
        p = self.data_dir.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


@lru_cache
def get_settings() -> Settings:
    return Settings()
