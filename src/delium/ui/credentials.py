"""Provider credential status for the UI — configured vs. missing only.

Reads `DeliumSecrets` and reports which providers are configured. It NEVER
returns or logs a secret value — only booleans and human labels. This is what
the UI's status panel and the per-page capability checks consume.
"""

from __future__ import annotations

from dataclasses import dataclass

from delium.config.secrets import DeliumSecrets, get_secrets


@dataclass(frozen=True)
class ProviderStatus:
    key: str  # stable id: 'keepa' | 'dataforseo' | 'reviews' | 'llm'
    label: str  # human label for the panel
    configured: bool  # whether the required credential(s) are present
    enables: str  # what functionality this credential unlocks


def _status_from(secrets: DeliumSecrets) -> list[ProviderStatus]:
    reviews = secrets.unwrangle_api_key is not None or secrets.apify_api_token is not None
    dataforseo = secrets.dataforseo_login is not None and secrets.dataforseo_password is not None
    return [
        ProviderStatus(
            "keepa",
            "Keepa",
            secrets.keepa_api_key is not None,
            "product facts, price/BSR history, validation & discovery scoring",
        ),
        ProviderStatus(
            "dataforseo",
            "DataForSEO",
            dataforseo,
            "keyword volume, related keywords, Amazon SERP (discovery)",
        ),
        ProviderStatus(
            "reviews",
            "Review provider (Unwrangle / Apify)",
            reviews,
            "review sampling → differentiation & Review Miner",
        ),
        ProviderStatus(
            "llm",
            "LLM (Anthropic)",
            secrets.llm_api_key is not None,
            "Review Miner, Analyst, Strategist (G5) — optional",
        ),
    ]


def provider_status() -> list[ProviderStatus]:
    """Current provider configuration, from the environment/.env. Values are
    never exposed — only whether each credential is present."""
    return _status_from(get_secrets())


def is_configured(key: str) -> bool:
    """True when the named provider ('keepa'|'dataforseo'|'reviews'|'llm') has its
    credential(s) configured."""
    return any(p.key == key and p.configured for p in provider_status())
