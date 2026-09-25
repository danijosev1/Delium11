"""Pre-flight cost estimates for UI actions that may hit paid providers.

Every estimate is deliberately conservative and, where possible, consults the
SAME cache the CLI/ingestion uses (`raw_fetches` + TTL) so a fresh cache hit is
reported as $0.00 — the UI must show this and require confirmation before any
paid call. Per-unit figures are the documented/observed spend estimates
(docs/data-economics.md); Keepa is a flat subscription so its marginal $ is 0
(it spends tokens instead). Nothing here makes a network call.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta

from delium.config.models import DeliumConfig
from delium.database import repository
from delium.ingestion.freshness import is_fresh
from delium.ingestion.keywords import keyword_request_key
from delium.providers.dataforseo import normalize_phrase

# Observed / documented spend estimates (USD).
_DFS_PER_KEYWORD_CALL = 0.01  # volume, related, serp — ~$0.03 for the trio
_REVIEW_PER_ASIN = 0.30  # ~100 reviews × ~$0.003


@dataclass(frozen=True)
class CostEstimate:
    """A pre-flight estimate for one UI action."""

    providers: tuple[str, ...]  # providers that WILL be called (empty if fully cached)
    est_low_usd: float
    est_high_usd: float
    fully_cached: bool
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_paid(self) -> bool:
        return self.est_high_usd > 0 and not self.fully_cached


def _product_request_key(marketplace: str, asin: str) -> str:
    # Mirrors ingestion.products._request_key (marketplace-scoped Keepa key).
    return f"keepa:product:{marketplace}:{asin}"


def _fresh(conn: sqlite3.Connection, provider: str, request_key: str, ttl: timedelta) -> bool:
    latest = repository.latest_raw_fetch(conn, provider, request_key)
    return latest is not None and is_fresh(latest["fetched_at"], ttl)


def keyword_estimate(
    conn: sqlite3.Connection,
    marketplace: str,
    seed: str,
    config: DeliumConfig,
    *,
    force: bool = False,
) -> CostEstimate:
    """DataForSEO volume + related + SERP for one seed. Predicts $0.00 when all
    three are already cached fresh (and not forced)."""
    seed_norm = normalize_phrase(seed)
    kw_ttl = timedelta(days=config.cache.keyword_ttl_days)
    serp_ttl = timedelta(days=config.cache.serp_ttl_days)
    checks = (
        ("volume", kw_ttl),
        ("related", kw_ttl),
        ("serp", serp_ttl),
    )
    to_fetch = 3
    if not force:
        cached = sum(
            _fresh(conn, "dataforseo", keyword_request_key(marketplace, endpoint, seed_norm), ttl)
            for endpoint, ttl in checks
        )
        to_fetch = 3 - cached
    if to_fetch == 0:
        return CostEstimate((), 0.0, 0.0, fully_cached=True, notes=("all three calls cached",))
    cost = round(to_fetch * _DFS_PER_KEYWORD_CALL, 4)
    return CostEstimate(
        ("DataForSEO",),
        cost,
        cost,
        fully_cached=False,
        notes=(f"{to_fetch} of 3 DataForSEO calls not cached",),
    )


def product_estimate(
    conn: sqlite3.Connection,
    marketplace: str,
    asin: str,
    config: DeliumConfig,
    *,
    force: bool = False,
) -> CostEstimate:
    """Keepa product fetch. Keepa is a flat subscription, so the marginal dollar
    cost is $0.00 — it spends Keepa tokens. Predicts a cache hit within TTL."""
    ttl = timedelta(hours=config.cache.product_ttl_hours)
    if not force and _fresh(conn, "keepa", _product_request_key(marketplace, asin), ttl):
        return CostEstimate((), 0.0, 0.0, fully_cached=True, notes=("product cached",))
    return CostEstimate(
        ("Keepa",),
        0.0,
        0.0,
        fully_cached=False,
        notes=("Keepa is a flat subscription — spends tokens, ~$0 marginal",),
    )


def validate_estimate(
    config: DeliumConfig,
    *,
    keepa: bool,
    dataforseo: bool,
    reviews: bool,
    llm: bool,
    force: bool = False,
) -> CostEstimate:
    """Full validation. Cache-first, so real spend is usually far below these
    caps; the band reflects the per-validate budget ceilings. Providers listed
    are those whose credentials are configured (others are skipped/degraded)."""
    providers: list[str] = []
    notes: list[str] = ["cache-first — already-fetched data is reused at no cost"]
    high = 0.0
    if keepa:
        providers.append("Keepa")
        notes.append("Keepa: target + competitors (tokens, ~$0 marginal)")
    if dataforseo:
        providers.append("DataForSEO")
    if reviews:
        providers.append("Review provider")
        high += min(_REVIEW_PER_ASIN * 4, config.budgets.max_data_usd_per_validate)
        notes.append(f"reviews capped at ${config.budgets.max_data_usd_per_validate:.2f}/validate")
    if llm:
        providers.append("LLM")
        high += config.budgets.max_llm_usd_per_validate
        notes.append(f"LLM capped at ${config.budgets.max_llm_usd_per_validate:.2f}/validate")
    if force:
        notes.append("force=on — caches bypassed, full spend likely")
    return CostEstimate(
        tuple(providers), 0.0, round(high, 2), fully_cached=False, notes=tuple(notes)
    )


def emerging_estimate(config: DeliumConfig, *, dataforseo: bool) -> tuple[CostEstimate, int, int]:
    """Emerging run estimate. Keepa is a flat subscription (spends tokens, ~$0
    marginal); returns (CostEstimate, finder_tokens, product_tokens_worst_case)
    so the UI can show the token budget explicitly."""
    from delium.discovery.emerging import estimate_tokens

    tokens = estimate_tokens(config)
    providers = ["Keepa"]
    notes = [
        f"Keepa Product Finder ~{tokens.finder_tokens} tokens + up to "
        f"{tokens.product_tokens_worst_case} product tokens (worst case; cache reused)",
        "Keepa is a flat subscription — spends tokens, ~$0 marginal",
    ]
    if dataforseo and config.emerging.enrich_top_n > 0:
        providers.append("DataForSEO")
        notes.append(f"top {config.emerging.enrich_top_n} may make DataForSEO keyword calls")
    est = CostEstimate(tuple(providers), 0.0, 0.0, fully_cached=False, notes=tuple(notes))
    return est, tokens.finder_tokens, tokens.product_tokens_worst_case


def discover_estimate(
    config: DeliumConfig, *, keyword_count: int, dataforseo: bool, keepa: bool
) -> CostEstimate:
    """Discovery: one DataForSEO keyword bundle per seed + Keepa hydration of
    surfaced candidates. Bounded by the per-discover data budget."""
    providers: list[str] = []
    if dataforseo:
        providers.append("DataForSEO")
    if keepa:
        providers.append("Keepa")
    high = round(
        min(keyword_count * 3 * _DFS_PER_KEYWORD_CALL, config.budgets.max_data_usd_per_discover), 2
    )
    return CostEstimate(
        tuple(providers),
        0.0,
        high,
        fully_cached=False,
        notes=(f"bounded by ${config.budgets.max_data_usd_per_discover:.2f}/discover budget",),
    )
