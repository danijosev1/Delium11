"""AI agents: Review Miner and Strategist (docs/agent-layer.md).

Hard rules: agents receive pre-fetched context only (no tool-calling), never call
`delium.providers`/`delium.ingestion` directly, and every output is schema-validated
with evidence-backed claims before it can reach persistence or a report. The LLM is
an extraction/judgment layer — it never sets a score, a frequency, or the
Buy/Test/Avoid verdict (scoring.py owns that; the Strategist only supplies the G5
concurrence, which can only block a would-be Buy).
"""

from delium.agents.analyst import (
    AnalystRun,
    persist_analyst_output,
    run_analyst,
)
from delium.agents.llm import (
    LlmAuthError,
    LlmClient,
    LlmError,
    LlmRateLimitError,
    LlmResponse,
    LlmResponseError,
    Tier,
    build_llm_client,
)
from delium.agents.miner import (
    MinerRun,
    persist_miner_output,
    run_review_miner,
)
from delium.agents.runner import AgentResult, run_structured
from delium.agents.schemas import AnalystReport, MinerReport, StrategistVerdict
from delium.agents.strategist import (
    derive_concurrence,
    run_strategist,
)

__all__ = [
    "AgentResult",
    "AnalystReport",
    "AnalystRun",
    "LlmAuthError",
    "LlmClient",
    "LlmError",
    "LlmRateLimitError",
    "LlmResponse",
    "LlmResponseError",
    "MinerReport",
    "MinerRun",
    "StrategistVerdict",
    "Tier",
    "build_llm_client",
    "derive_concurrence",
    "persist_analyst_output",
    "persist_miner_output",
    "run_analyst",
    "run_review_miner",
    "run_strategist",
    "run_structured",
]
