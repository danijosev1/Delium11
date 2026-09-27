"""Research Profile — the seller's shared preferences (Phase 1 UI).

A profile is DB-backed *preference* state layered on top of the engine. It sets
the defaults for every finder/search and every profit calculation, and it steers
how results are sorted/highlighted. It never relaxes a hard kill, gate, or
scoring weight — those stay in `config` + `scoring.py`.
"""

from delium.profile.models import CogsMode, ProfitProfile, ResearchProfile, RiskTolerance

__all__ = ["CogsMode", "ProfitProfile", "ResearchProfile", "RiskTolerance"]
