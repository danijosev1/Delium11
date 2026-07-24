"""AI agents: Scout, Analyst, Review Miner, Strategist (docs/agent-layer.md).

Not yet implemented. Hard rules once it is: agents receive pre-fetched
context only (no tool-calling), never call `delium.providers` directly, and
every output is schema-validated with evidence-backed claims before it can
reach a report.
"""
