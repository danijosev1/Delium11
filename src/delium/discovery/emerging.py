"""Emerging-products discovery orchestration.

Funnel: Keepa Product Finder (recent + selling + low-review candidates) → batched
cache-first hydration → deterministic emergence signal (why it's emerging) → the
EXISTING hard kills + scoring.py for each candidate → rank by emergence → persist.

Boundaries this module keeps (identical to the discovery pipeline):
- scoring.py is the SOLE owner of the opportunity verdict; this reuses
  `pipeline._evaluate` (cheap-kill-first → full scoring) and never re-implements a
  score, kill, or gate. The emergence score is a separate, explanatory signal.
- providers are reached only through the Keepa client (finder) and ingestion
  (cache-first, batched product hydration).
- no LLM; the only wall clock is the Product Finder's "recent" cutoff (a query
  input, never a scoring input — scoring's `as_of` stays data-derived).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from math import log10
from typing import cast

from delium.analysis.curves import theil_sen
from delium.analysis.emerging import (
    EmergenceInput,
    EmergenceSignal,
    build_finder_selection,
    compute_emergence,
    load_emerging_data,
)
from delium.analysis.models import Marketplace
from delium.config.models import DeliumConfig
from delium.database import repository
from delium.discovery.models import (
    Candidate,
    CandidateOutcome,
    DiscoveryEvidence,
    DiscoverySource,
    EvaluatedCandidate,
)
from delium.discovery.pipeline import DfsFactory, KeepaFactory, _evaluate
from delium.providers.base import ProviderError
from delium.providers.keepa import KeepaClient, finder_token_estimate
from delium.utils.logging import get_logger

log = get_logger(__name__)

_PRODUCT_TOKENS_EST = 4  # worst-case Keepa tokens per product (history+stats+rating)
_MOMENTUM_WINDOW_DAYS = 90


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EmergingCandidate:
    """One emerging candidate: its emergence signal + the EXISTING scoring outcome."""

    evaluated: EvaluatedCandidate
    emergence: EmergenceSignal
    keyword_volume: int | None = None

    @property
    def asin(self) -> str:
        return self.evaluated.asin

    @property
    def outcome(self) -> CandidateOutcome:
        return self.evaluated.outcome


@dataclass(frozen=True)
class EmergingReport:
    run_id: str
    marketplace: Marketplace
    ranked: tuple[EmergingCandidate, ...]  # scored survivors, emergence-ordered
    killed: tuple[EmergingCandidate, ...]  # emerging-but-hard-killed, with reasons
    unresolved: tuple[EmergingCandidate, ...]
    finder_total_results: int | None
    finder_tokens: int
    product_tokens: int
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class TokenEstimate:
    finder_tokens: int
    product_tokens_worst_case: int

    @property
    def total_worst_case(self) -> int:
        return self.finder_tokens + self.product_tokens_worst_case


def estimate_tokens(config: DeliumConfig) -> TokenEstimate:
    """Pre-flight Keepa token estimate for one emerging run (worst case: every
    Product Finder result is a cache miss needing a product fetch)."""
    page = config.emerging.page_size
    return TokenEstimate(finder_token_estimate(page), page * _PRODUCT_TOKENS_EST)


# ---------------------------------------------------------------------------
# Emergence input assembly (reads persisted history — deterministic)
# ---------------------------------------------------------------------------
def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _latest(rows: list[sqlite3.Row], column: str) -> int | None:
    for row in reversed(rows):
        if row[column] is not None:
            return int(row[column])
    return None


def _bsr_slope_90d(rows: list[sqlite3.Row], latest: date) -> tuple[float | None, int | None]:
    """Theil–Sen slope of log10(BSR) per day over the trailing window, plus the
    observed history span in days. Negative slope = rank improving."""
    dated = [
        (d, int(r["bsr"]))
        for r in rows
        if r["bsr"] is not None and int(r["bsr"]) > 0 and (d := _parse_date(r["captured_on"]))
    ]
    if len(dated) < 2:
        return None, (0 if not dated else None)
    earliest = min(d for d, _ in dated)
    history_days = (latest - earliest).days
    window = [(d, b) for d, b in dated if (latest - d).days <= _MOMENTUM_WINDOW_DAYS]
    if len(window) < 2:
        return None, history_days
    xs = [float((d - earliest).days) for d, _ in window]
    ys = [log10(b) for _, b in window]
    return theil_sen(xs, ys), history_days


def _emergence_input(conn: sqlite3.Connection, asin: str, as_of: date) -> EmergenceInput:
    rows = repository.get_price_bsr_history(conn, asin)
    dates = [d for r in rows if (d := _parse_date(r["captured_on"]))]
    earliest = min(dates) if dates else None
    latest = max(dates) if dates else as_of
    slope, history_days = _bsr_slope_90d(rows, latest)
    return EmergenceInput(
        asin=asin,
        as_of=as_of,
        earliest_history=earliest,
        current_bsr=_latest(rows, "bsr"),
        bsr_slope_90d=slope,
        review_count=_latest(rows, "review_count"),
        history_days=history_days,
    )


# ---------------------------------------------------------------------------
# Optional DataForSEO enrichment (best-effort; never blocks the run)
# ---------------------------------------------------------------------------
def _title_seed(conn: sqlite3.Connection, asin: str, marketplace: str) -> str | None:
    row = repository.get_product(conn, asin, marketplace)
    title = row["title"] if row is not None else None
    if not title:
        return None
    words = [w for w in title.split() if w.isalnum() or "-" in w][:4]
    return " ".join(words) or None


def _enrich_keyword(
    conn: sqlite3.Connection,
    asin: str,
    marketplace: str,
    config: DeliumConfig,
    run_id: str,
    dfs_factory: DfsFactory,
) -> int | None:
    """Best-effort: fetch the seed volume for a keyword derived from the product
    title. Cache-first; a provider failure yields None (never raises)."""
    from delium.ingestion import fetch_keywords
    from delium.providers.dataforseo import DataForSeoClient

    seed = _title_seed(conn, asin, marketplace)
    if seed is None:
        return None
    client = cast(DataForSeoClient, dfs_factory(marketplace))
    try:
        result = fetch_keywords(seed, run_id=run_id, client=client, config=config)
    except ProviderError:
        return None
    return result.seed_volume


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run_emerging(
    conn: sqlite3.Connection,
    *,
    marketplace: Marketplace,
    category_ids: list[int],
    config: DeliumConfig,
    run_id: str,
    keepa_factory: KeepaFactory | None,
    dfs_factory: DfsFactory | None = None,
    overrides: dict[str, int] | None = None,
    as_of: date | None = None,
    persist: bool = True,
) -> EmergingReport:
    """Find emerging products, score them through the existing pipeline, and rank
    by emergence. Requires a Keepa factory (the Product Finder is Keepa-only);
    without it the run returns empty with a note rather than raising."""
    data = load_emerging_data(config.emerging.data_version)
    as_of = as_of or date.today()

    if keepa_factory is None:
        return EmergingReport(
            run_id,
            marketplace,
            (),
            (),
            (),
            None,
            0,
            0,
            ("Keepa is not configured — the emerging search needs the Product Finder.",),
        )

    client = cast(KeepaClient, keepa_factory(marketplace.value))
    selection = build_finder_selection(
        data,
        as_of=as_of,
        category_ids=category_ids,
        per_page=config.emerging.page_size,
        overrides=overrides,
    )
    finder = client.product_finder(selection)
    asins = list(finder.asins[: config.emerging.page_size])

    from delium.ingestion import hydrate_products

    views = hydrate_products(asins, run_id=run_id, client=client, config=config)
    product_tokens = sum(v.tokens_used for v in views.values())

    signals: dict[str, EmergenceSignal] = {}
    for asin in asins:
        if asin in views and views[asin].found:
            signals[asin] = compute_emergence(_emergence_input(conn, asin, as_of), data.emergence)

    ranked_asins = sorted(
        signals,
        key=lambda a: (signals[a].emergence_score is None, -(signals[a].emergence_score or 0.0), a),
    )[: config.emerging.top_n]

    kw_volume: dict[str, int | None] = {}
    if dfs_factory is not None and config.emerging.enrich_top_n > 0:
        for asin in ranked_asins[: config.emerging.enrich_top_n]:
            kw_volume[asin] = _enrich_keyword(
                conn, asin, marketplace.value, config, run_id, dfs_factory
            )

    candidates: list[EmergingCandidate] = []
    for asin in ranked_asins:
        candidate = Candidate(
            asin=asin,
            marketplace=marketplace,
            evidence=(
                DiscoveryEvidence(
                    source=DiscoverySource.EXPLICIT,
                    reference="emerging",
                    detail=f"emerging:{marketplace.value}",
                ),
            ),
        )
        evaluated, _prov = _evaluate(conn, candidate, config)
        candidates.append(
            EmergingCandidate(evaluated, signals[asin], keyword_volume=kw_volume.get(asin))
        )

    ranked = tuple(c for c in candidates if c.outcome is CandidateOutcome.SCORED)
    killed = tuple(c for c in candidates if c.outcome is CandidateOutcome.KILLED)
    unresolved = tuple(c for c in candidates if c.outcome is CandidateOutcome.UNRESOLVED)

    report = EmergingReport(
        run_id=run_id,
        marketplace=marketplace,
        ranked=ranked,
        killed=killed,
        unresolved=unresolved,
        finder_total_results=finder.total_results,
        finder_tokens=finder.tokens_consumed,
        product_tokens=product_tokens,
    )
    if persist:
        _persist(conn, report, category_ids, config)
    return report


def _persist(
    conn: sqlite3.Connection,
    report: EmergingReport,
    category_ids: list[int],
    config: DeliumConfig,
) -> None:
    repository.insert_emerging_run(
        conn,
        run_id=report.run_id,
        marketplace=report.marketplace.value,
        categories=category_ids,
        page_size=config.emerging.page_size,
        top_n=config.emerging.top_n,
        finder_total_results=report.finder_total_results,
        finder_tokens=report.finder_tokens + report.product_tokens,
    )
    for c in (*report.ranked, *report.killed, *report.unresolved):
        scored = c.evaluated.scored
        repository.insert_emerging_candidate(
            conn,
            run_id=report.run_id,
            asin=c.asin,
            marketplace=report.marketplace.value,
            emergence_score=c.emergence.emergence_score,
            age_days=c.emergence.age_days,
            outcome=c.outcome.value,
            opportunity_score=scored.score if scored is not None else None,
            verdict=scored.verdict.value if scored is not None else None,
            confidence=scored.confidence.level.value if scored is not None else None,
            kill_rule=c.evaluated.kill_rule,
            signals={
                "subsignals": [
                    {"name": s.name, "score": s.score, "weight": s.weight, "detail": s.detail}
                    for s in c.emergence.subsignals
                ],
                "keyword_volume": c.keyword_volume,
            },
        )
