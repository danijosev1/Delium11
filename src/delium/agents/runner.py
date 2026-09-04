"""Agent runner — the shared guard rail for every LLM agent (docs/agent-layer.md §5).

One prompt in, one validated typed object out. The runner owns exactly the
guarantees the deterministic layer relies on:

- **Structured output only.** The model's text is parsed and validated against a
  pydantic schema; a validation failure retries once with the error appended,
  then the step fails (never invents a fallback).
- **Evidence or discard.** A per-agent `evidence_check` mechanically drops array
  items whose citations don't resolve and reports how many were dropped; when
  more than the configured share is dropped, the runner retries once, then fails.
- **Graceful degradation.** Any LLM/transport error or an unparseable response
  returns `failed` (the pipeline keeps the deterministic result) — it is never
  turned into fabricated evidence.
- **Untrusted-text firewall.** The runner never executes model output; it only
  parses it into inert data. Prompts (built by the agents) delimit customer text.

Cost and model/provider provenance from every attempt (retries included) are
accumulated for the audit trail.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from delium.agents.llm import LlmClient, LlmError, LlmResponse, Tier
from delium.config.models import AgentsConfig
from delium.utils.logging import get_logger

log = get_logger(__name__)


# evidence_check(output) -> (cleaned_output, dropped_items, total_items)
type EvidenceCheck[T: BaseModel] = Callable[[T], tuple[T, int, int]]


@dataclass(frozen=True)
class AgentResult[TModel: BaseModel]:
    """The outcome of one agent invocation, with full provenance for auditing."""

    status: str  # "ok" | "degraded" | "failed"
    output: TModel | None
    model: str | None
    provider: str | None
    tier: str | None
    cost_usd: float
    tokens_in: int
    tokens_out: int
    dropped: int
    total: int
    error: str | None

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "degraded") and self.output is not None


def run_structured[T: BaseModel](
    client: LlmClient,
    *,
    tier: Tier,
    system: str,
    user: str,
    schema: type[T],
    config: AgentsConfig,
    evidence_check: EvidenceCheck[T] | None = None,
) -> AgentResult[T]:
    """Run one agent: call → parse → validate → evidence-resolve, with a single
    retry on any recoverable failure. Cost accrues across attempts."""
    cost = 0.0
    tok_in = 0
    tok_out = 0
    model: str | None = None
    provider: str | None = None
    last_error = "no attempts made"
    prompt = user

    for _attempt in range(config.max_retries + 1):
        try:
            resp: LlmResponse = client.complete(tier=tier, system=system, user=prompt)
        except LlmError as exc:
            # Provider/transport failure is terminal — degrade, never fabricate.
            return AgentResult(
                "failed", None, model, provider, tier.value, cost, tok_in, tok_out, 0, 0, str(exc)
            )
        cost += resp.cost_usd
        tok_in += resp.input_tokens
        tok_out += resp.output_tokens
        model, provider = resp.model, resp.provider

        if resp.refused:
            last_error = "model refused the request"
            prompt = _retry_prompt(user, last_error)
            continue

        parsed, parse_err = _parse(resp.text, schema)
        if parsed is None:
            last_error = parse_err or "schema validation failed"
            prompt = _retry_prompt(user, last_error)
            continue

        dropped, total = 0, 0
        if evidence_check is not None:
            parsed, dropped, total = evidence_check(parsed)
            if total > 0 and dropped / total > config.evidence_drop_threshold:
                last_error = f"{dropped}/{total} items had unresolvable evidence"
                prompt = _retry_prompt(user, last_error)
                continue

        status = "degraded" if dropped > 0 else "ok"
        return AgentResult(
            status, parsed, model, provider, tier.value, cost, tok_in, tok_out, dropped, total, None
        )

    return AgentResult(
        "failed", None, model, provider, tier.value, cost, tok_in, tok_out, 0, 0, last_error
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _parse[T: BaseModel](text: str, schema: type[T]) -> tuple[T | None, str | None]:
    """Extract the first JSON object from the model's text and validate it against
    `schema`. Returns (model, None) on success or (None, error) on any failure —
    malformed JSON or a schema violation never raises out of here."""
    obj = _extract_json_object(text)
    if obj is None:
        return None, "no JSON object found in response"
    try:
        return schema.model_validate(obj), None
    except ValidationError as exc:
        return None, f"schema validation failed: {exc.errors(include_url=False)[:5]}"


def _extract_json_object(text: str) -> dict[str, object] | None:
    """Find the first balanced JSON object in `text`, tolerating code fences and
    surrounding prose. Uses raw_decode so trailing commentary is ignored."""
    if not text:
        return None
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except ValueError:
            start = text.find("{", start + 1)
            continue
        return obj if isinstance(obj, dict) else None
    return None


def _retry_prompt(original: str, error: str) -> str:
    """Re-ask with the specific failure appended (agent-layer §5.2)."""
    return (
        f"{original}\n\n"
        f"IMPORTANT: your previous response was rejected — {error}. "
        "Return ONLY one valid JSON object that matches the required schema exactly, "
        "with every themed item citing at least three real review ids from the sample."
    )
