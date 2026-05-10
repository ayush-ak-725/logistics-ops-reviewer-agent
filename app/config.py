from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = Field(
        default="sqlite:///./freight_bill_agent.db",
        description="SQLAlchemy database URL. Docker uses Postgres; sqlite is useful for quick local tests.",
    )
    seed_data_path: Path = Path("data/seed_data_logistics.json")
    auto_seed: bool = True
    log_level: str = "INFO"
    llm_provider: str = "ollama"
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    openai_model: str = "gpt-4o-mini"
    ollama_api_key: str | None = Field(default=None, validation_alias="OLLAMA_API_KEY")
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:8b"
    llm_timeout_seconds: float = 60.0
    enable_llm_explanations: bool = False
    enable_llm_carrier_normalization: bool = False

    auto_approve_threshold: float = 0.88
    review_threshold: float = 0.55
    currency_tolerance: float = 1.0
    rate_tolerance_percent: float = 0.01
    weight_tolerance_kg: float = 1.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
