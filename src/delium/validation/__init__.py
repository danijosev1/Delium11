"""Deterministic validation pipeline (ARCHITECTURE.md §4.2).

Public surface: the orchestrator `run_validation`, its request/report models, the
hydration `Clients` injection point, and the reproducible `validation_snapshot`.
scoring.py remains the sole owner of the Buy/Test/Avoid verdict; the LLM Review
Miner and Strategist (G5) are downstream and unimplemented — never faked here.
"""

from delium.validation.hydration import Clients
from delium.validation.models import (
    HydrationOutcome,
    ReviewEvidence,
    ValidationReport,
    ValidationRequest,
    ValidationStatus,
)
from delium.validation.pipeline import run_validation, validation_snapshot

__all__ = [
    "Clients",
    "HydrationOutcome",
    "ReviewEvidence",
    "ValidationReport",
    "ValidationRequest",
    "ValidationStatus",
    "run_validation",
    "validation_snapshot",
]
