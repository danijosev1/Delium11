"""Secrets and environment-derived settings.

API keys never belong in config.toml (which may end up in a report's
methodology snapshot or get committed by accident). They are read from
environment variables — or a local `.env` file, which is git-ignored — via
pydantic-settings.

Providers (`providers/keepa.py`, `providers/dataforseo.py`, etc.) and the LLM
client are the only V1 consumers of this module; none of them are
implemented yet, but the settings surface is defined now so those modules
have a single, typed place to read credentials from.
"""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DeliumSecrets(BaseSettings):
    """Environment-backed secrets. All fields optional at foundation stage —
    a missing key only matters once the module that needs it is implemented
    and actually called.
    """

    model_config = SettingsConfigDict(
        env_prefix="DELIUM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    keepa_api_key: SecretStr | None = None
    dataforseo_login: SecretStr | None = None
    dataforseo_password: SecretStr | None = None
    unwrangle_api_key: SecretStr | None = None  # primary review provider
    apify_api_token: SecretStr | None = None  # fallback review provider
    llm_api_key: SecretStr | None = None


def get_secrets() -> DeliumSecrets:
    """Load secrets from the environment / .env file. Cheap; call freely."""
    return DeliumSecrets()
