"""LLM client for the agent layer — a thin, injectable Anthropic Messages client.

Mirrors the provider layer (`providers/base.py`): HTTP lives behind an injectable
`PostTransport`, so agents contain only prompt/parse logic and tests inject a fake
transport instead of touching the network — there are NO live LLM calls in tests.
Consistent with the project's zero-HTTP-dependency design; no new dependency.

Model *tier* (fast/frontier) is config, not code (docs/agent-layer.md §6). Cost is
computed from the response's token usage against config pricing — auditable, never
guessed. The client is stateless and does no validation of content: the agent
runner owns schema validation, evidence resolution, and retries.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from delium.config.models import AgentsConfig
from delium.providers.base import (
    HttpResult,
    PostTransport,
    ProviderError,
    UrllibTransport,
)
from delium.utils.logging import get_logger

log = get_logger(__name__)

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"


class LlmError(Exception):
    """Base class for LLM-layer failures. All are caught by the agent runner and
    degrade the run to the deterministic result — never fabricated evidence."""


class LlmAuthError(LlmError):
    """Authentication rejected (401/403)."""


class LlmRateLimitError(LlmError):
    """Rate limit hit (429) — retryable."""


class LlmResponseError(LlmError):
    """Unexpected status or unparseable/empty body."""


class Tier(StrEnum):
    FAST = "fast"  # Scout / Analyst / Review Miner (Haiku-class)
    FRONTIER = "frontier"  # Strategist only (Sonnet-class)


@dataclass(frozen=True)
class LlmResponse:
    """One completion plus the provenance the persistence/report layers need."""

    text: str
    model: str
    provider: str
    tier: Tier
    input_tokens: int
    output_tokens: int
    cost_usd: float
    stop_reason: str | None

    @property
    def refused(self) -> bool:
        return self.stop_reason == "refusal"


@dataclass(frozen=True)
class _TierSpec:
    model: str
    max_tokens: int
    input_usd_per_mtok: float
    output_usd_per_mtok: float


class LlmClient:
    """Anthropic Messages client. `complete` returns text + usage/cost; it never
    validates or repairs content — that is the runner's job."""

    provider = "anthropic"

    def __init__(
        self,
        api_key: str,
        config: AgentsConfig,
        *,
        transport: PostTransport | None = None,
        base_url: str = _API_URL,
    ) -> None:
        self._api_key = api_key
        self._config = config
        self._transport = transport or UrllibTransport()
        self._base_url = base_url

    def _spec(self, tier: Tier) -> _TierSpec:
        c = self._config
        if tier is Tier.FRONTIER:
            return _TierSpec(
                c.frontier_model,
                c.max_output_tokens_frontier,
                c.frontier_input_usd_per_mtok,
                c.frontier_output_usd_per_mtok,
            )
        return _TierSpec(
            c.fast_model,
            c.max_output_tokens_fast,
            c.fast_input_usd_per_mtok,
            c.fast_output_usd_per_mtok,
        )

    def complete(self, *, tier: Tier, system: str, user: str) -> LlmResponse:
        """One request/one response (no tool-calling, per agent-layer §5.1)."""
        spec = self._spec(tier)
        body = {
            "model": spec.model,
            "max_tokens": spec.max_tokens,
            "temperature": self._config.temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _API_VERSION,
        }
        try:
            result = self._transport.post_json(self._base_url, body, headers)
        except ProviderError as exc:  # transport-level (network/timeout)
            raise LlmError(f"LLM transport error: {exc}") from exc

        return self._parse(result, spec, tier)

    def _parse(self, result: HttpResult, spec: _TierSpec, tier: Tier) -> LlmResponse:
        if result.status in (401, 403):
            raise LlmAuthError("LLM authentication rejected")
        if result.status == 429:
            raise LlmRateLimitError("LLM rate limit exceeded")
        if result.status >= 500:
            raise LlmResponseError(f"LLM server error (HTTP {result.status})")
        if result.status != 200:
            raise LlmResponseError(f"unexpected LLM status HTTP {result.status}")

        body = result.body
        if not isinstance(body, dict):
            raise LlmResponseError("LLM response body was not a JSON object")
        text = _extract_text(body.get("content"))
        usage = body.get("usage") or {}
        in_tokens = int(usage.get("input_tokens", 0) or 0)
        out_tokens = int(usage.get("output_tokens", 0) or 0)
        cost = (
            in_tokens / 1_000_000 * spec.input_usd_per_mtok
            + out_tokens / 1_000_000 * spec.output_usd_per_mtok
        )
        return LlmResponse(
            text=text,
            model=str(body.get("model", spec.model)),
            provider=self.provider,
            tier=tier,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cost_usd=cost,
            stop_reason=body.get("stop_reason"),
        )


def _extract_text(content: Any) -> str:
    """Concatenate the text blocks of a Messages API response."""
    if not isinstance(content, list):
        return ""
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return "".join(parts)


def build_llm_client(
    config: AgentsConfig, *, transport: PostTransport | None = None
) -> LlmClient | None:
    """Construct an LLM client from the environment secret, or None when no
    credential is configured — agents then never run and the pipeline degrades to
    the deterministic result."""
    from delium.config.secrets import get_secrets

    secret = get_secrets().llm_api_key
    if secret is None:
        return None
    return LlmClient(secret.get_secret_value(), config, transport=transport)
