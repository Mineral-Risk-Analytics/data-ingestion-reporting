"""Application settings from environment (Pydantic Settings v2)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://bdi:bdi@localhost:5432/battery_intel"
    # Direct (unpooled) connection used by Alembic. Falls back to database_url if not set.
    database_url_direct: str = ""
    storage_root: str = "./storage"

    federal_register_base_url: str = "https://www.federalregister.gov/api/v1"
    federal_register_user_agent: str = (
        "battery-intel-ingestion/0.1 (https://example.com/contact)"
    )

    census_api_key: str = ""
    census_trade_base_url: str = "https://api.census.gov/data/timeseries/intltrade"

    sec_edgar_base_url: str = "https://data.sec.gov"
    sec_edgar_user_agent: str = "battery-intel-ingestion contact@example.com"

    news_provider: str = "stub"
    news_api_key: str = ""

    # OpenAI / embedding
    openai_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding_batch_size: int = 64  # max texts per API call

    # UN Comtrade API
    comtrade_api_key: str = ""
    comtrade_base_url: str = "https://comtradeapi.un.org/data/v1/get"
    comtrade_rate_limit_delay: float = 1.0  # seconds between API calls


@lru_cache
def get_settings() -> Settings:
    return Settings()
