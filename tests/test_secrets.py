from __future__ import annotations

import pytest

from delium.config.secrets import get_secrets


def test_secrets_default_to_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "DELIUM_KEEPA_API_KEY",
        "DELIUM_DATAFORSEO_LOGIN",
        "DELIUM_DATAFORSEO_PASSWORD",
        "DELIUM_UNWRANGLE_API_KEY",
        "DELIUM_APIFY_API_TOKEN",
        "DELIUM_LLM_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    secrets = get_secrets()

    assert secrets.keepa_api_key is None
    assert secrets.unwrangle_api_key is None
    assert secrets.apify_api_token is None
    assert secrets.llm_api_key is None


def test_secrets_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELIUM_KEEPA_API_KEY", "test-keepa-key")
    monkeypatch.setenv("DELIUM_LLM_API_KEY", "test-llm-key")

    secrets = get_secrets()

    assert secrets.keepa_api_key is not None
    assert secrets.keepa_api_key.get_secret_value() == "test-keepa-key"
    assert secrets.llm_api_key is not None
    assert secrets.llm_api_key.get_secret_value() == "test-llm-key"
