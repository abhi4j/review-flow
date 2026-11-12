from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

JsonMode = Literal["json_schema", "json_object"]


class Settings(BaseSettings):
    """Runtime configuration, read from the environment (and `.env` in development).

    Secrets are `SecretStr` so they never appear in reprs, logs or error messages.
    """

    # Empty values (e.g. `LLM_API_KEY=` in .env) mean "unset", not "empty string".
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    github_token: SecretStr

    database_path: Path = Path("data/reviewflow.db")

    # Ingestion
    sync_ttl_seconds: int = Field(600, ge=0)
    github_page_size: int = Field(50, ge=1, le=100)
    github_min_rate_budget: int = Field(100, ge=0)

    # Query limits
    default_window_days: int = Field(30, ge=1)
    max_window_days: int = Field(180, ge=1)
    # How far back `since` may reach. Older periods need a walk through the whole PR
    # history since then, which one request should not be able to trigger.
    max_lookback_days: int = Field(365, ge=1)
    # Comma-separated "owner/repo" list. Empty means any public repository.
    allowed_repos: Annotated[frozenset[str], NoDecode] = frozenset()

    # Narrative: any OpenAI-compatible Chat Completions API, enabled when a key is set.
    llm_api_key: SecretStr | None = None
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"
    # json_object is supported almost everywhere; json_schema constrains decoding where the
    # provider supports it. See reviewflow/narrative/llm.py.
    llm_json_mode: JsonMode = "json_object"
    llm_timeout_seconds: float = Field(120.0, gt=0)

    log_level: str = "INFO"

    @model_validator(mode="before")
    @classmethod
    def _parse_allowed_repos(cls, data: dict[str, object]) -> dict[str, object]:
        raw = data.get("allowed_repos")
        if isinstance(raw, str):
            data["allowed_repos"] = frozenset(
                item.strip().lower() for item in raw.split(",") if item.strip()
            )
        return data

    @model_validator(mode="after")
    def _check_window_limits(self) -> Self:
        if self.default_window_days > self.max_window_days:
            raise ValueError("DEFAULT_WINDOW_DAYS must not exceed MAX_WINDOW_DAYS")
        if self.max_window_days > self.max_lookback_days:
            raise ValueError("MAX_WINDOW_DAYS must not exceed MAX_LOOKBACK_DAYS")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # populated from the environment
