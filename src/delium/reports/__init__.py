"""Report rendering: typed blocks → Markdown (docs/agent-layer.md §6).

`render_validation` composes a report from typed objects only (the
`ValidationReport`: deterministic `ScoredOpportunity` + validated agent outputs) —
never from freeform model text. Deterministic numbers are separated from advisory
Strategist narrative, model/review text is neutralized (untrusted-text firewall),
and a report is regenerable from stored data with no LLM call.
"""

from delium.reports.render import quote_ids, render_validation

__all__ = ["quote_ids", "render_validation"]
