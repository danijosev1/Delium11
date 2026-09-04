"""Cache-first hydration for validation — the only path to providers is through
`ingestion/` (never a provider client directly), so cache TTLs and the
raw_fetches spend ledger are always honored (docs/data-layer.md §3, ARCHITECTURE
§4.2 "all cached").

Marketplace is preserved end to end: product/keyword fetches carry the client's
marketplace, and a cached row for marketplace A can never satisfy marketplace B
(the ingestion cache keys and DB reads are marketplace-scoped). Reviews are
ASIN-scoped by contract, and ASINs differ per marketplace, so they are isolated
in practice too.

`--force` is threaded to ingestion, which is the layer that decides what a force
re-fetches. This module performs no analysis and computes no score.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from delium.agents.llm import LlmClient
from delium.analysis.models import Marketplace
from delium.config.models import DeliumConfig
from delium.database import repository
from delium.ingestion import fetch_keywords, fetch_product, fetch_reviews
from delium.ingestion.reviews import ReviewSource
from delium.providers import ProviderError
from delium.utils.logging import get_logger

log = get_logger(__name__)

KeepaFactory = Callable[[str], object]
DfsFactory = Callable[[str], object]

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
_URL_ASIN_RE = re.compile(r"/(?:dp|gp/product|product|gp/aw/d)/([A-Z0-9]{10})", re.IGNORECASE)


@dataclass(frozen=True)
class Clients:
    """Provider factories/clients injected by the CLI (real) or tests (fakes).
    Any may be None — hydration then runs on already-cached data only, and a None
    `llm` means the agent layer never runs (deterministic-only, `miner_pending`)."""

    keepa: KeepaFactory | None = None
    dfs: DfsFactory | None = None
    reviews: ReviewSource | None = None
    llm: LlmClient | None = None


@dataclass(frozen=True)
class TargetResolution:
    asin: str | None
    seed: str | None  # primary keyword cluster seed, when known
    cost_usd: float = 0.0
    error: str | None = None  # 'invalid' | 'no_cluster_data'


@dataclass(frozen=True)
class ProductHydration:
    found: bool
    from_cache: bool | None
    cost_usd: float = 0.0
    error: str | None = None  # 'not_found' | 'provider_error' | 'no_provider_no_cache'


@dataclass(frozen=True)
class FetchTally:
    cost_usd: float = 0.0
    count: int = 0
    from_cache: bool | None = None
    provider: str | None = None
    degraded: bool = False
    notes: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Target resolution: ASIN | Amazon URL | keyword → target ASIN
# ---------------------------------------------------------------------------
def resolve_target(
    conn: sqlite3.Connection,
    target: str,
    marketplace: Marketplace,
    config: DeliumConfig,
    run_id: str,
    clients: Clients,
    *,
    force: bool = False,
) -> TargetResolution:
    """Resolve the raw input to a target ASIN. A bare ASIN or an Amazon URL maps
    directly; a keyword is resolved via its SERP (cache-first) by picking the top
    organic ASIN. Input parsing is a regex, never a model (ARCHITECTURE §5)."""
    raw = target.strip()
    if not raw:
        return TargetResolution(asin=None, seed=None, error="invalid")

    url_match = _URL_ASIN_RE.search(raw)
    if url_match:
        asin = url_match.group(1).upper()
        return TargetResolution(asin=asin, seed=_primary_seed(conn, asin, marketplace))

    if _ASIN_RE.match(raw.upper()) and any(ch.isdigit() for ch in raw):
        asin = raw.upper()
        return TargetResolution(asin=asin, seed=_primary_seed(conn, asin, marketplace))

    # Otherwise treat as a keyword seed → SERP → top organic ASIN.
    from delium.providers.dataforseo import normalize_phrase

    seed = normalize_phrase(raw)
    cost = 0.0
    if clients.dfs is not None:
        try:
            result = fetch_keywords(
                seed,
                run_id=run_id,
                client=clients.dfs(marketplace.value),  # type: ignore[arg-type]
                config=config,
                force=force,
            )
            cost += result.cost_usd
        except ProviderError as exc:
            log.info("keyword resolution fetch failed for %r: %s", seed, exc)
    rankings = repository.get_serp_rankings(conn, seed, marketplace.value)
    organic = [r for r in rankings if not r["sponsored"]]
    if not organic:
        return TargetResolution(asin=None, seed=seed, cost_usd=cost, error="no_cluster_data")
    return TargetResolution(asin=organic[0]["asin"], seed=seed, cost_usd=cost)


def _primary_seed(conn: sqlite3.Connection, asin: str, marketplace: Marketplace) -> str | None:
    phrases = repository.get_serp_keyword_phrases(conn, asin, marketplace.value)
    return phrases[0] if phrases else None


# ---------------------------------------------------------------------------
# Product (target) hydration — cache-first
# ---------------------------------------------------------------------------
def ensure_product(
    conn: sqlite3.Connection,
    asin: str,
    marketplace: Marketplace,
    config: DeliumConfig,
    run_id: str,
    clients: Clients,
    *,
    force: bool = False,
) -> ProductHydration:
    """Ensure the target product exists (cache-first). With no Keepa client we
    can only use what's already cached for THIS marketplace."""
    if clients.keepa is None:
        row = repository.get_product(conn, asin, marketplace.value)
        if row is None:
            return ProductHydration(found=False, from_cache=None, error="no_provider_no_cache")
        return ProductHydration(found=True, from_cache=True)

    try:
        view = fetch_product(
            asin,
            run_id=run_id,
            client=clients.keepa(marketplace.value),  # type: ignore[arg-type]
            config=config,
            force=force,
        )
    except ProviderError as exc:
        log.info("target product fetch failed for %s [%s]: %s", asin, marketplace.value, exc)
        # Fall back to a cached row if we have one, else surface the provider error.
        if repository.get_product(conn, asin, marketplace.value) is not None:
            return ProductHydration(found=True, from_cache=True, error=None)
        return ProductHydration(found=False, from_cache=None, error="provider_error")

    if view is None or not view.found:
        return ProductHydration(found=False, from_cache=None, error="not_found")
    return ProductHydration(found=True, from_cache=view.from_cache, cost_usd=view.cost_usd)


# ---------------------------------------------------------------------------
# Cluster + competitor hydration — cache-first
# ---------------------------------------------------------------------------
def hydrate_cluster(
    conn: sqlite3.Connection,
    asin: str,
    seed: str | None,
    marketplace: Marketplace,
    config: DeliumConfig,
    run_id: str,
    clients: Clients,
    *,
    force: bool = False,
) -> FetchTally:
    """Fetch the keyword cluster (volumes) and the SERP competitor pool's Keepa
    stats (cache-first). No seed / no clients → operate on cached data only."""
    if seed is None:
        seed = _primary_seed(conn, asin, marketplace)
    if seed is None:
        return FetchTally(notes=("no keyword cluster seed for target",))

    cost = 0.0
    notes: list[str] = []
    if clients.dfs is not None:
        try:
            result = fetch_keywords(
                seed,
                run_id=run_id,
                client=clients.dfs(marketplace.value),  # type: ignore[arg-type]
                config=config,
                force=force,
            )
            cost += result.cost_usd
        except ProviderError as exc:
            notes.append(f"keyword fetch failed: {exc}")

    hydrated = 0
    competitor_asins = [
        r["asin"]
        for r in repository.get_serp_rankings(conn, seed, marketplace.value)[
            : config.discovery.max_candidates_per_seed
        ]
    ]
    if clients.keepa is not None:
        for comp_asin in competitor_asins:
            try:
                fetch_product(
                    comp_asin,
                    run_id=run_id,
                    client=clients.keepa(marketplace.value),  # type: ignore[arg-type]
                    config=config,
                    force=force,
                )
                hydrated += 1
            except ProviderError as exc:
                notes.append(f"competitor {comp_asin} fetch failed: {exc}")
    return FetchTally(cost_usd=cost, count=hydrated, notes=tuple(notes))


# ---------------------------------------------------------------------------
# Review hydration — cache-first, budget-capped (validate-tier spend)
# ---------------------------------------------------------------------------
def hydrate_reviews(
    asins: list[str],
    config: DeliumConfig,
    run_id: str,
    clients: Clients,
    *,
    budget_usd: float,
    force: bool = False,
) -> FetchTally:
    """Fetch reviews for the target + top competitors (cache-first). Stops once
    the per-run data budget is exhausted and marks the tally `degraded`."""
    if clients.reviews is None:
        return FetchTally(notes=("no review provider — using cached reviews only",))

    cost = 0.0
    fetched = 0
    degraded = False
    notes: list[str] = []
    any_cache: bool | None = None
    provider: str | None = None
    for asin in asins:
        if cost >= budget_usd:
            degraded = True
            notes.append("review budget exhausted — remaining competitors skipped")
            break
        try:
            result = fetch_reviews(
                asin, run_id=run_id, provider=clients.reviews, config=config, force=force
            )
        except ProviderError as exc:
            notes.append(f"review fetch failed for {asin}: {exc}")
            degraded = True
            continue
        cost += result.cost_usd
        fetched += 1
        any_cache = result.from_cache if any_cache is None else (any_cache and result.from_cache)
        provider = provider or result.provider
    return FetchTally(
        cost_usd=cost,
        count=fetched,
        from_cache=any_cache,
        provider=provider,
        degraded=degraded,
        notes=tuple(notes),
    )
