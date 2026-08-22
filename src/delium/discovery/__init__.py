"""Deterministic discovery / scout orchestration layer.

Orchestrates existing deterministic components (providers via ingestion,
analysis engines, scoring) into a ranked research queue. Not analysis itself —
this package may touch ingestion/database/scoring, but never adds LLM, network,
randomness, or wall-clock logic to scoring decisions. scoring.py remains the
sole owner of the opportunity verdict.
"""

from delium.discovery.models import (
    Candidate,
    CandidateOutcome,
    DiscoveryEvidence,
    DiscoveryReport,
    DiscoverySource,
    EvaluatedCandidate,
)
from delium.discovery.pipeline import (
    candidates_from_cross_market,
    candidates_from_explicit,
    candidates_from_keyword,
    deduplicate,
    rank,
    run_discovery,
)

__all__ = [
    "Candidate",
    "CandidateOutcome",
    "DiscoveryEvidence",
    "DiscoveryReport",
    "DiscoverySource",
    "EvaluatedCandidate",
    "candidates_from_cross_market",
    "candidates_from_explicit",
    "candidates_from_keyword",
    "deduplicate",
    "rank",
    "run_discovery",
]
