"""Test helpers for the LLM agent layer: a fake POST transport (no network) plus
builders for Anthropic-style response bodies and valid Miner/Strategist JSON."""

from __future__ import annotations

import json
from typing import Any

from delium.agents.llm import LlmClient
from delium.config.models import AgentsConfig
from delium.providers.base import HttpResult


def llm_body(
    text: str,
    *,
    input_tokens: int = 1000,
    output_tokens: int = 200,
    model: str = "claude-haiku-4-5",
    stop_reason: str = "end_turn",
) -> HttpResult:
    """A 200 Messages-API response whose single text block is `text`."""
    return HttpResult(
        status=200,
        body={
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            "model": model,
            "stop_reason": stop_reason,
        },
    )


def json_body(payload: Any, **kw: Any) -> HttpResult:
    return llm_body(json.dumps(payload), **kw)


def http(status: int, body: Any = None) -> HttpResult:
    return HttpResult(status=status, body=body if body is not None else {})


class FakeLlmTransport:
    """Returns queued results in order; the last repeats for extra calls. A queued
    item may be an HttpResult (returned) or an Exception (raised)."""

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.calls: list[dict[str, Any]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def post_json(self, url: str, body: Any, headers: Any) -> HttpResult:
        self.calls.append({"url": url, "body": body, "headers": dict(headers)})
        index = min(len(self.calls) - 1, len(self._results) - 1)
        item = self._results[index]
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, HttpResult)
        return item


class RoutingLlmTransport:
    """Routes by agent: the Miner prompt contains 'voice-of-customer', the
    Strategist prompt contains 'private-label Amazon operator'. Each side is an
    HttpResult or an Exception."""

    def __init__(self, *, miner: Any, strategist: Any) -> None:
        self._miner = miner
        self._strategist = strategist
        self.calls: list[dict[str, Any]] = []

    def post_json(self, url: str, body: Any, headers: Any) -> HttpResult:
        self.calls.append({"url": url, "body": body, "headers": dict(headers)})
        system = body.get("system", "")
        item = self._miner if "voice-of-customer" in system else self._strategist
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, HttpResult)
        return item


def build_llm(transport: Any, config: AgentsConfig | None = None) -> LlmClient:
    return LlmClient("test-key", config or AgentsConfig(), transport=transport)


# ---------------------------------------------------------------------------
# Valid payload builders
# ---------------------------------------------------------------------------
def miner_payload(
    *,
    complaint_ids: list[str],
    theme: str = "lid cracks when frozen",
    severity: int = 3,
    category: str | None = "usage",
    cogs_impact: str = "low",
    feature_ids: list[str] | None = None,
    bundle_ids: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "complaints": [
            {
                "theme": theme,
                "severity": severity,
                "quote_review_ids": complaint_ids,
                "representative_quote_ids": complaint_ids[:2],
                "affects_asins": [],
                "category": category,
            }
        ],
        "praise": [],
        "missing_features": [],
        "improvement_ideas": [
            {"idea": "thicker lid", "addresses_theme": theme, "cogs_impact_guess": cogs_impact}
        ],
        "bundle_signals": [],
        "sample_caveats": ["sample skews positive"],
    }
    if feature_ids:
        payload["missing_features"] = [
            {"feature": "silicone lid", "requested_in_review_ids": feature_ids}
        ]
    if bundle_ids:
        payload["bundle_signals"] = [
            {"complement": "storage bag", "mentioned_in_review_ids": bundle_ids}
        ]
    return payload


def strategist_payload(*, verdict: str = "buy", agrees: bool = True) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "conviction": 4,
        "agrees_with_score": agrees,
        "rationale": [
            {"point": "weak incumbents", "evidence": ["competition"]},
            {"point": "fixable complaint", "evidence": ["differentiation"]},
            {"point": "healthy margin", "evidence": ["profit"]},
        ],
        "differentiation_plan": [
            {
                "change": "thicker lid",
                "addresses": "lid cracks",
                "cogs_impact": "low",
                "defensibility_note": "process improvement",
            }
        ],
        "launch_shape": {
            "suggested_price_ref": "market_median",
            "inventory_posture": "standard",
            "primary_keyword_ref": "baby food tray",
        },
        "risk_register": [
            {
                "risk": "CPSIA testing",
                "likelihood": "M",
                "impact": "H",
                "mitigation": "budget testing",
                "evidence": ["risk"],
            },
            {
                "risk": "seasonality",
                "likelihood": "L",
                "impact": "M",
                "mitigation": "watch",
                "evidence": ["demand"],
            },
        ],
        "verdict_changers": [
            {"fact_that_would_flip": "moat rises", "how_to_obtain_it": "recheck keepa"},
            {"fact_that_would_flip": "cogs higher", "how_to_obtain_it": "supplier quote"},
        ],
        "assumption_challenges": [
            {"assumption_flag_ref": "product_cost", "why_questionable": "silicone volatile"}
        ],
        "one_paragraph": "Promising differentiated play in a beatable market; test before scaling.",
    }
