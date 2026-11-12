import pytest
from pydantic import ValidationError

from reviewflow.config import Settings


def _env(monkeypatch, **values):
    for key in ("LLM_API_KEY", "ALLOWED_REPOS", "LLM_MODEL", "DEFAULT_WINDOW_DAYS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_empty_values_mean_unset(monkeypatch):
    _env(monkeypatch, LLM_API_KEY="", ALLOWED_REPOS="", LLM_MODEL="")
    settings = Settings(_env_file=None)
    assert settings.llm_api_key is None
    assert settings.allowed_repos == frozenset()
    assert settings.llm_model == "deepseek-flash"


def test_allowed_repos_are_normalised(monkeypatch):
    _env(monkeypatch, ALLOWED_REPOS="FastAPI/fastapi, pydantic/Pydantic ,")
    assert Settings(_env_file=None).allowed_repos == {"fastapi/fastapi", "pydantic/pydantic"}


def test_default_window_must_fit_the_limit(monkeypatch):
    _env(monkeypatch, DEFAULT_WINDOW_DAYS="365")
    with pytest.raises(ValidationError, match="DEFAULT_WINDOW_DAYS"):
        Settings(_env_file=None)


def test_secrets_are_not_in_repr(monkeypatch):
    _env(monkeypatch, GITHUB_TOKEN="ghp_secret_value", LLM_API_KEY="llm-secret")
    text = repr(Settings(_env_file=None))
    assert "ghp_secret_value" not in text and "llm-secret" not in text


def test_window_limit_must_fit_the_lookback(monkeypatch):
    _env(monkeypatch, MAX_WINDOW_DAYS="400")
    with pytest.raises(ValidationError, match="MAX_LOOKBACK_DAYS"):
        Settings(_env_file=None)
