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

    # World Bank Pink Sheet (commodity prices)
    # The URL includes two embedded year-and-publication identifiers that
    # change annually when the World Bank issues a new vintage (typically
    # in January): a per-publication doc hash AND a visible YYYY slug.
    # Both parts must be updated when the hardcoded URL 404s.  Override
    # via the WORLDBANK_PINK_SHEET_URL env var so the operator can fix
    # this without a code change.  The default below tracks the current
    # 2026 publication.  See worldbank_pinksheet.PINK_SHEET_URL and the
    # discovery-hint logging in download_pink_sheet for the operator
    # path when the URL breaks.
    worldbank_pink_sheet_url: str = (
        "https://thedocs.worldbank.org/en/doc/74e8be41ceb20fa0da750cda2f6b9e4e-0050012026"
        "/related/CMO-Historical-Data-Monthly.xlsx"
    )
    # Landing page used for discovery-hint scraping when the configured
    # download URL returns 404.  Only consulted on failure; never used as
    # the primary source.
    worldbank_pink_sheet_landing_page: str = (
        "https://www.worldbank.org/en/research/commodity-markets"
    )

    # UN Comtrade API
    comtrade_api_key: str = ""
    comtrade_base_url: str = "https://comtradeapi.un.org/data/v1/get"
    comtrade_rate_limit_delay: float = 1.0  # seconds between API calls
    # Max records per call.  Tier-dependent: 100,000 for Basic Individual
    # (our current tier), 250,000 for Premium tiers.  Bump when upgrading
    # the subscription so we don't silently bottleneck.
    comtrade_max_records: int = 100_000

    # Clerk auth (JWT via JWKS)
    # When clerk_jwks_url is empty AND app_env == "development", auth is bypassed
    # and a stub admin user is injected. Any non-empty value enforces verification.
    clerk_jwks_url: str = ""
    clerk_issuer: str = ""
    clerk_audience: str = ""
    # Frontend origin(s) allowed by CORS. Comma-separated for multiple origins.
    frontend_url: str = "http://localhost:3000"


@lru_cache
def get_settings() -> Settings:
    return Settings()
