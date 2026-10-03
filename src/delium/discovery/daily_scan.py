"""Daily Scan pipeline — a resumable, budget-gated funnel (docs: README).

Stages (0..8): preflight → sweep → hydrate → normalize+kill → cheap score →
competitor sets → rank → finalists → report. Every stage persists its progress
(scan row `stage` + `scan_candidates` + a `scan_stages` funnel row with token/$
cost), so a crash resumes from the last completed stage. Preflight projects the
cost of every later stage and aborts BEFORE spending if it would exceed the
Keepa-token or USD caps.

Boundaries: providers are reached only through `ingestion` (cache-first, batched)
and, for finalists, the existing `validation` pipeline (reviews + Claude
differentiation + re-score). scoring.py stays the sole verdict owner; kills and
gates are never loosened; "missing = unknown" is preserved. No provider clients
are constructed here — the caller injects factories, so tests use fakes.

Keepa has no Australia data: a requested AU marketplace is recorded as
"AU pending" and skipped; the scan continues with the others.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from delium.analysis.category_momentum import (
    MomentumProduct,
    compute_category_momentum,
    load_momentum_data,
)
from delium.analysis.emerging import (
    build_finder_selections,
    is_established_brand,
    load_emerging_data,
)
from delium.analysis.launchability import Competitor, compute_launchability, load_launchability_data
from delium.analysis.models import Confidence, Marketplace
from delium.analysis.scoring import score_opportunity
from delium.analysis.sellability import (
    SellabilityInput,
    compute_sellability,
    load_sellability_data,
    product_momentum_score,
)
from delium.config.models import DeliumConfig
from delium.database import repository
from delium.discovery.assembly import build_scoring_input
from delium.discovery.dedupe import group_by_parent
from delium.profile.models import ResearchProfile
from delium.utils.logging import get_logger

log = get_logger(__name__)

# Keepa has no Australia coverage.
_NO_KEEPA = frozenset({"AU"})

# Fix B — projected data cost of a light-mode finalist: reviews for the finalist
# ONLY (a small sample), no competitor reviews. A fraction of a full validate's
# review spend; used only for the preflight projection (actual cost is metered).
_LIGHT_REVIEW_USD = 0.30

STAGE_NAMES = (
    "preflight",
    "sweep",
    "hydrate",
    "normalize_kill",
    "cheap_score",
    "competitor_sets",
    "rank",
    "finalists",
    "report",
)

_CONF_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}
_KeepaFactory = Callable[[str], Any]
_DfsFactory = Callable[[str], Any]
# Finalist enricher: reviews + Claude differentiation + re-score for one ASIN.
# Returns (opportunity_score|None, differentiation_available, data_usd, llm_usd).
_Enricher = Callable[[str, str, str], "EnrichResult"]


@dataclass(frozen=True)
class EnrichResult:
    opportunity_score: float | None
    differentiation_available: bool
    data_usd: float
    llm_usd: float
    eligible: bool = True


@dataclass(frozen=True)
class ScanClients:
    keepa_factory: _KeepaFactory | None = None
    dfs_factory: _DfsFactory | None = None
    # Optional finalist enricher (reviews + Claude differentiation + re-score).
    # None → finalists show "differentiation pending", never a fabricated score.
    enrich_finalist: _Enricher | None = None


@dataclass(frozen=True)
class ScanParams:
    marketplaces: tuple[str, ...] = ("US",)
    sweep_target: int = 300  # max ASINs to bring back from the sweep
    per_slice: int = 100  # finder results per category slice
    competitor_pool: int = 40  # top-by-cheap-score that get a competitor set
    top_n: int = 10  # finalists
    budget_cap_tokens: int | None = None  # abort if projected Keepa tokens exceed
    max_spend_usd: float | None = None  # abort if projected USD exceed
    min_confidence: Confidence = Confidence.LOW
    scheduled: bool = False  # non-interactive; abort (never prompt) if over caps
    cross_market: bool = True
    # Fix B — finalist enrichment cost control:
    #  light_finalists: reviews for the finalist ONLY (no competitors) + a single
    #    Claude call each; the enricher reuses cached reviews/analysis. Default for
    #    --scheduled runs. The full validate path stays available for the Workspace
    #    "Deep dive".
    #  enrich_limit: only the top N finalists are enriched; the rest keep
    #    differentiation_status "pending" ("differentiation pending" in the UI).
    light_finalists: bool = False
    enrich_limit: int = 5
    # Optional, OFF by default: also run a small zombie-listing pass (out-of-stock
    # but still reviewed) for the same marketplaces and attach a summary to the
    # report. Additive — it never changes the main funnel or verdicts.
    include_zombies: bool = False
    zombie_sweep: int = 50


@dataclass(frozen=True)
class StageCost:
    stage: int
    name: str
    keepa_tokens: int
    data_usd: float
    llm_usd: float


@dataclass(frozen=True)
class CostProjection:
    stages: tuple[StageCost, ...]
    notes: tuple[str, ...] = ()

    @property
    def total_tokens(self) -> int:
        return sum(s.keepa_tokens for s in self.stages)

    @property
    def total_usd(self) -> float:
        return round(sum(s.data_usd + s.llm_usd for s in self.stages), 2)

    def over_caps(self, params: ScanParams) -> str | None:
        if params.budget_cap_tokens is not None and self.total_tokens > params.budget_cap_tokens:
            return (
                f"projected {self.total_tokens} Keepa tokens exceeds --budget-cap "
                f"{params.budget_cap_tokens}"
            )
        if params.max_spend_usd is not None and self.total_usd > params.max_spend_usd:
            return (
                f"projected ${self.total_usd:.2f} exceeds --max-spend ${params.max_spend_usd:.2f}"
            )
        return None


class ScanAbortedError(Exception):
    """Raised in preflight when the projection exceeds a cap (before any spend)."""


class ScanError(Exception):
    """Raised when a stage cannot produce a usable result (e.g. the sweep returned
    0 ASINs because every finder call errored). The scan is marked 'failed' with
    this reason — distinct from a legitimate empty result, which completes."""


# Keepa Product Finder fields we send. The CORE set (price band + sales-rank band
# + paging) is the minimum a sweep needs; everything else is an OPTIONAL filter
# that a fallback retry drops if Keepa rejects the full selection (HTTP 400), so
# one bad optional filter can never kill the whole sweep.
_CORE_FINDER_KEYS = frozenset(
    {
        "current_NEW_gte",
        "current_NEW_lte",
        "current_SALES_gte",
        "current_SALES_lte",
        "productType",
        "page",
        "perPage",
    }
)


def _core_finder_selection(selection: dict[str, Any]) -> dict[str, Any]:
    """The selection stripped to its core keys — used to retry once after a 400
    so an unsupported/rejected optional filter can't kill the sweep."""
    return {k: v for k, v in selection.items() if k in _CORE_FINDER_KEYS}


# ---------------------------------------------------------------------------
# Cost projection (pure)
# ---------------------------------------------------------------------------
def project_costs(
    params: ScanParams, profile: ResearchProfile, config: DeliumConfig
) -> CostProjection:
    """Project Keepa tokens + USD for every spending stage, before spending a
    thing. Conservative (worst-case cache-miss) so the caps bind on the high side."""
    from delium.providers.keepa import finder_token_estimate

    active_mps = [m for m in params.marketplaces if m not in _NO_KEEPA]
    notes: list[str] = []
    for m in params.marketplaces:
        if m in _NO_KEEPA:
            notes.append(f"{m} pending — Keepa has no {m} data; it will be skipped.")

    data = load_emerging_data(config.emerging.data_version)
    slices = max(1, len(profile.category_ids()) or 1)
    sub_bands = max(1, data.finder.sub_bands)
    per_band = max(1, -(-params.per_slice // sub_bands))
    # Sweep: one finder call per (marketplace, slice, sub-band).
    sweep_tokens = len(active_mps) * slices * sub_bands * finder_token_estimate(per_band)
    # Hydrate: ~2 tokens per swept ASIN (1 base + 1 rating), worst case all missed.
    hydrate_tokens = params.sweep_target * 2
    # Competitor sets: 3 DataForSEO keyword calls + Keepa hydration of the SERP set.
    dfs_call = 0.01
    serp_depth = config.discovery.serp_depth
    comp_usd = round(params.competitor_pool * 3 * dfs_call, 2)
    comp_tokens = params.competitor_pool * serp_depth * 2
    # Finalists: reviews + LLM + one keyword bundle each, for the enriched subset.
    #   full mode  → every finalist runs the full validate path (competitor
    #                reviews + Analyst + Strategist): worst-case validate cost each.
    #   light mode → only the top `enrich_limit` finalists, each finalist-only
    #                reviews (a fraction of a full validate) + a single Claude call;
    #                the rest are reported "differentiation pending" (no spend).
    if params.light_finalists:
        n_enriched = max(0, min(params.top_n, params.enrich_limit))
        fin_data = round(n_enriched * (_LIGHT_REVIEW_USD + 3 * dfs_call), 2)
        fin_llm = round(n_enriched * config.budgets.max_llm_usd_per_validate, 2)
    else:
        fin_data = round(
            params.top_n * (config.budgets.max_data_usd_per_validate + 3 * dfs_call), 2
        )
        fin_llm = round(params.top_n * config.budgets.max_llm_usd_per_validate, 2)

    stages = [
        StageCost(1, "sweep", sweep_tokens, 0.0, 0.0),
        StageCost(2, "hydrate", hydrate_tokens, 0.0, 0.0),
        StageCost(5, "competitor_sets", comp_tokens, comp_usd, 0.0),
        StageCost(7, "finalists", 0, fin_data, fin_llm),
    ]
    if params.include_zombies:
        from delium.providers.keepa import finder_token_estimate as _fte

        z_tokens = len(active_mps) * _fte(params.zombie_sweep) + params.zombie_sweep * 2
        stages.append(StageCost(9, "zombies", z_tokens, 0.0, 0.0))
    return CostProjection(stages=tuple(stages), notes=tuple(notes))


# ---------------------------------------------------------------------------
# Stage bookkeeping
# ---------------------------------------------------------------------------
@dataclass
class _Ledger:
    """Tracks Keepa tokens + provider $ spent so far on the scan's run_id, so each
    stage can log its own delta."""

    run_id: str
    tokens: int = 0
    usd: float = 0.0

    def snapshot(self, conn: sqlite3.Connection) -> None:
        self.tokens = repository.run_token_total(conn, self.run_id)
        self.usd = repository.run_cost_total(conn, self.run_id)

    def delta(self, conn: sqlite3.Connection) -> tuple[int, float]:
        t = repository.run_token_total(conn, self.run_id)
        u = repository.run_cost_total(conn, self.run_id)
        d = (t - self.tokens, round(u - self.usd, 4))
        self.tokens, self.usd = t, u
        return d


@dataclass
class ScanReport:
    scan_id: str
    status: str
    marketplaces: tuple[str, ...]
    funnel: dict[str, int] = field(default_factory=dict)
    stages: list[dict[str, Any]] = field(default_factory=list)
    finalists: list[dict[str, Any]] = field(default_factory=list)
    categories: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    keepa_tokens: int = 0
    data_usd: float = 0.0
    llm_usd: float = 0.0


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run_scan(
    conn: sqlite3.Connection,
    *,
    params: ScanParams,
    profile: ResearchProfile,
    config: DeliumConfig,
    clients: ScanClients,
    confirm: Callable[[CostProjection], bool] | None = None,
    as_of: date | None = None,
    resume_scan_id: str | None = None,
) -> ScanReport:
    """Run (or resume) a daily scan. Each completed stage is persisted so a crash
    resumes from `scans.stage + 1`."""
    as_of = as_of or date.today()
    if resume_scan_id is not None:
        scan_row = repository.get_scan(conn, resume_scan_id)
        if scan_row is None:
            raise ValueError(f"no scan {resume_scan_id!r} to resume")
        scan_id, run_id = scan_row["id"], scan_row["run_id"]
        last_done = int(scan_row["stage"])
        notes = list(_loads(scan_row["notes"]) or [])
    else:
        run_id = repository.insert_run(conn, command="scan", input_=",".join(params.marketplaces))
        scan_id = repository.create_scan(
            conn,
            run_id=run_id,
            marketplaces=list(params.marketplaces),
            profile_id=profile.id,
            profile_name=profile.name,
            params=_params_json(params),
        )
        last_done = -1
        notes = []
    repository.update_scan(conn, scan_id, status="running")

    ledger = _Ledger(run_id)
    ledger.snapshot(conn)
    active_mps = tuple(m for m in params.marketplaces if m not in _NO_KEEPA)
    for m in params.marketplaces:
        if m in _NO_KEEPA and f"{m} pending" not in " ".join(notes):
            notes.append(f"{m} pending — Keepa has no {m} data; skipped.")

    stages: list[Callable[[], _StageOut]] = [
        lambda: _stage_preflight(
            conn, scan_id, params, profile, config, clients, active_mps, notes
        ),
        lambda: _stage_sweep(
            conn, scan_id, run_id, params, profile, config, clients, active_mps, as_of
        ),
        lambda: _stage_hydrate(conn, scan_id, run_id, params, config, clients),
        lambda: _stage_normalize_kill(conn, scan_id, config, profile),
        lambda: _stage_cheap_score(conn, scan_id, config, profile),
        lambda: _stage_competitor_sets(conn, scan_id, run_id, params, config, profile, clients),
        lambda: _stage_rank(conn, scan_id, params, config, profile),
        lambda: _stage_finalists(conn, scan_id, run_id, params, profile, config, clients),
        lambda: _stage_report(conn, scan_id, config),
    ]

    try:
        for stage_idx, fn in enumerate(stages):
            if stage_idx <= last_done:
                continue
            name = STAGE_NAMES[stage_idx]
            repository.upsert_scan_stage(
                conn, scan_id=scan_id, stage=stage_idx, name=name, status="running", started=True
            )
            out = fn()
            # Interactive confirmation happens right after preflight (which spends
            # nothing but the free token check) and before any paid stage.
            if (
                stage_idx == 0
                and not params.scheduled
                and confirm is not None
                and not confirm(project_costs(params, profile, config))
            ):
                raise ScanAbortedError("declined at confirmation")
            dt, du = ledger.delta(conn)
            repository.upsert_scan_stage(
                conn,
                scan_id=scan_id,
                stage=stage_idx,
                name=name,
                status="skipped" if out.skipped else "complete",
                input_count=out.input_count,
                output_count=out.output_count,
                killed_count=out.killed_count,
                keepa_tokens=dt,
                data_usd=du,
                llm_usd=out.llm_usd,
                detail=out.detail,
                finished=True,
            )
            notes.extend(out.notes)
            repository.update_scan(conn, scan_id, stage=stage_idx, notes=notes)
    except ScanAbortedError as exc:
        notes.append(f"aborted: {exc}")
        repository.update_scan(conn, scan_id, status="aborted", notes=notes)
        conn.commit()  # persist the aborted state before the caller's rollback
        raise
    except Exception as exc:  # noqa: BLE001 - persist failure state, then re-raise for the CLI
        notes.append(f"failed: {exc}")
        repository.update_scan(conn, scan_id, status="failed", notes=notes)
        conn.commit()  # persist progress + failed state before the caller's rollback
        raise

    if params.include_zombies and clients.keepa_factory is not None:
        try:
            z_note = _run_zombie_pass(conn, params, config, clients, as_of)
            notes.append(z_note)
            repository.update_scan(conn, scan_id, notes=notes)
        except Exception as exc:  # noqa: BLE001 - the optional pass never fails the scan
            notes.append(f"zombie pass skipped: {exc}")
            repository.update_scan(conn, scan_id, notes=notes)

    total_tokens = repository.run_token_total(conn, run_id)
    total_usd = repository.run_cost_total(conn, run_id)
    llm_total = sum(s["llm_usd"] for s in repository.get_scan_stages(conn, scan_id))
    repository.update_scan(
        conn,
        scan_id,
        status="complete",
        keepa_tokens=total_tokens,
        data_usd=round(total_usd, 4),
        llm_usd=round(llm_total, 4),
        notes=notes,
    )
    repository.finish_run(conn, run_id, status="complete")
    return build_report(conn, scan_id)


@dataclass
class _StageOut:
    input_count: int = 0
    output_count: int = 0
    killed_count: int = 0
    llm_usd: float = 0.0
    detail: Any = None
    notes: list[str] = field(default_factory=list)
    skipped: bool = False


# --- Stage 0: preflight -----------------------------------------------------
def _stage_preflight(
    conn: sqlite3.Connection,
    scan_id: str,
    params: ScanParams,
    profile: ResearchProfile,
    config: DeliumConfig,
    clients: ScanClients,
    active_mps: tuple[str, ...],
    notes: list[str],
) -> _StageOut:
    projection = project_costs(params, profile, config)
    detail: dict[str, Any] = {
        "projected_tokens": projection.total_tokens,
        "projected_usd": projection.total_usd,
        "sweep_size": params.sweep_target,
        "competitor_sets": params.competitor_pool,
        "per_stage": [
            {"stage": s.name, "tokens": s.keepa_tokens, "usd": round(s.data_usd + s.llm_usd, 2)}
            for s in projection.stages
        ],
    }
    # Free Keepa token check.
    tokens_left: int | None = None
    refill_rate: int | None = None
    if clients.keepa_factory is not None and active_mps:
        try:
            status = clients.keepa_factory(active_mps[0]).token_status()
            tokens_left = status.tokens_left
            refill_rate = status.refill_rate
        except Exception:  # noqa: BLE001 - a token check must never crash preflight
            tokens_left = None
    detail["keepa_tokens_left"] = tokens_left

    # A cap is the only hard limit. The Keepa BALANCE is not: the client paces
    # (waits for refills) when it runs low, so a projection that exceeds the
    # current balance just means the scan will spend time waiting, not that it
    # cannot complete. Abort only when the projection exceeds the token/USD caps.
    over = projection.over_caps(params)
    if over is not None:
        raise ScanAbortedError(over)
    if not active_mps:
        raise ScanAbortedError("no Keepa-covered marketplaces requested")
    if tokens_left is not None and projection.total_tokens > tokens_left:
        deficit = projection.total_tokens - tokens_left
        detail["pace_deficit_tokens"] = deficit
        if refill_rate and refill_rate > 0:
            pace_minutes = -(-deficit // refill_rate)  # ceil
            detail["pace_minutes"] = pace_minutes
            notes.append(
                f"will pace: projected {projection.total_tokens} tokens exceeds the current "
                f"Keepa balance {tokens_left}; ~{pace_minutes} min waiting for refills "
                f"(~{refill_rate} tokens/min). The scan continues — token pacing handles the waits."
            )
        else:
            notes.append(
                f"will pace: projected {projection.total_tokens} tokens exceeds the current "
                f"Keepa balance {tokens_left}; the scan waits for refills as it runs."
            )
    out = _StageOut(input_count=0, output_count=len(active_mps), detail=detail)
    out.notes = list(projection.notes)
    return out


# --- shared helpers ---------------------------------------------------------
def _loads(value: str | None) -> Any:
    import json

    if not value:
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def _params_json(params: ScanParams) -> dict[str, Any]:
    return {
        "marketplaces": list(params.marketplaces),
        "sweep_target": params.sweep_target,
        "per_slice": params.per_slice,
        "competitor_pool": params.competitor_pool,
        "top_n": params.top_n,
        "budget_cap_tokens": params.budget_cap_tokens,
        "max_spend_usd": params.max_spend_usd,
        "min_confidence": params.min_confidence.value,
        "cross_market": params.cross_market,
        "light_finalists": params.light_finalists,
        "enrich_limit": params.enrich_limit,
    }


def _latest(rows: list[sqlite3.Row], col: str) -> int | None:
    for r in reversed(rows):
        if r[col] is not None:
            return int(r[col])
    return None


def _latest_float(rows: list[sqlite3.Row], col: str) -> float | None:
    for r in reversed(rows):
        if r[col] is not None:
            return float(r[col])
    return None


def _iso(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _age_days(rows: list[sqlite3.Row], as_of: date) -> int | None:
    dates = [d for r in rows if (d := _iso(r["captured_on"])) is not None]
    return (as_of - min(dates)).days if dates else None


def _own_review_velocity(rows: list[sqlite3.Row], as_of: date) -> float | None:
    """The candidate's OWN new-reviews/month over its trailing ≤90d window."""
    pts = [
        (d, int(r["review_count"]))
        for r in rows
        if r["review_count"] is not None and (d := _iso(r["captured_on"])) is not None
    ]
    pts = [(d, c) for d, c in pts if (as_of - d).days <= 120]
    if len(pts) < 2:
        return None
    pts.sort()
    (d0, c0), (d1, c1) = pts[0], pts[-1]
    days = (d1 - d0).days
    if days <= 0 or c1 < c0:
        return None
    return round((c1 - c0) / days * 30.0, 2)


def _target_price_cents(profile: ResearchProfile, fallback: int | None) -> int | None:
    lo, hi = profile.price_min_cents, profile.price_max_cents
    if lo is not None and hi is not None and hi >= lo:
        return (lo + hi) // 2
    return fallback


def _row_get(row: sqlite3.Row, col: str) -> Any:
    try:
        return row[col]
    except (IndexError, KeyError):
        return None


def _build_competitor(
    conn: sqlite3.Connection, asin: str, marketplace: str, as_of: date
) -> Competitor:
    row = repository.get_product(conn, asin, marketplace)
    hist = repository.get_price_bsr_history(conn, asin)
    return Competitor(
        asin=asin,
        reviews=_latest(hist, "review_count"),
        rating=_latest_float(hist, "rating"),
        price_cents=_latest(hist, "price_cents"),
        images_count=_row_get(row, "images_count") if row is not None else None,
        title=row["title"] if row is not None else None,
        brand=row["brand"] if row is not None else None,
        age_days=_age_days(hist, as_of),
    )


def _emergence_score(
    conn: sqlite3.Connection, asin: str, as_of: date, config: DeliumConfig
) -> float | None:
    try:
        from delium.analysis.emerging import compute_emergence, load_emerging_data
        from delium.discovery.emerging import _emergence_input

        data = load_emerging_data(config.emerging.data_version)
        return compute_emergence(
            _emergence_input(conn, asin, as_of), data.emergence
        ).emergence_score
    except Exception:  # noqa: BLE001 - emergence is advisory
        return None


# --- Stage 1: sweep ---------------------------------------------------------
def _stage_sweep(
    conn: sqlite3.Connection,
    scan_id: str,
    run_id: str,
    params: ScanParams,
    profile: ResearchProfile,
    config: DeliumConfig,
    clients: ScanClients,
    active_mps: tuple[str, ...],
    as_of: date,
) -> _StageOut:
    data = load_emerging_data(config.emerging.data_version)
    overrides = dict(profile.finder_overrides())
    seen: set[tuple[str, str]] = set()
    swept = 0
    finder_calls = 0
    finder_errors = 0
    last_error: str | None = None
    notes: list[str] = []

    for mp in active_mps:
        if clients.keepa_factory is None or swept >= params.sweep_target:
            break
        client = clients.keepa_factory(mp)
        # Category ids are marketplace-specific — only apply the profile's ids to
        # the marketplace(s) they were declared for; otherwise omit with a note.
        cat_ids, cat_note = _category_ids_for(profile, mp)
        if cat_note is not None:
            notes.append(cat_note)
        slices: list[int] = cat_ids or [0]  # 0 → broad (no category filter)
        for slice_id in slices:
            cats = [slice_id] if slice_id else []
            selections = build_finder_selections(
                data, as_of=as_of, category_ids=cats, per_page=params.per_slice, overrides=overrides
            )
            for sel in selections:
                if swept >= params.sweep_target:
                    break
                # Optional finder-level K3 proxy (grams); dropped by the fallback.
                if profile.max_weight_g is not None and profile.max_weight_g > 0:
                    sel["packageWeight_lte"] = int(profile.max_weight_g)
                finder, note = _run_finder_with_fallback(client, sel, mp=mp, slice_id=slice_id)
                if note is not None:
                    notes.append(note)
                if finder is None:
                    finder_errors += 1
                    last_error = note
                    continue
                finder_calls += 1
                for asin in finder.asins:
                    key = (asin, mp)
                    if key in seen or swept >= params.sweep_target:
                        continue
                    seen.add(key)
                    swept += 1
                    repository.upsert_scan_candidate(
                        conn,
                        scan_id=scan_id,
                        asin=asin,
                        marketplace=mp,
                        outcome="swept",
                        stage_reached=1,
                        source="finder",
                    )

    xm_count = 0
    if params.cross_market and len(active_mps) > 1:
        from delium.ingestion import discover_cross_market

        for target in active_mps:
            sources = tuple(Marketplace(m) for m in active_mps if m != target)
            for src in sources:
                try:
                    cands = discover_cross_market(
                        conn,
                        source_mp=src,
                        target_mps=(Marketplace(target),),
                        config=config,
                        run_id=run_id,
                        persist_matches=False,
                    )
                except Exception as exc:  # noqa: BLE001 - cross-market reads DB only; never fatal
                    log.warning("cross-market pass failed: %s", exc)
                    continue
                for c in cands:
                    asin = c.report.match.target.asin
                    key = (asin, target)
                    if key in seen:
                        continue
                    seen.add(key)
                    xm_count += 1
                    swept += 1
                    repository.upsert_scan_candidate(
                        conn,
                        scan_id=scan_id,
                        asin=asin,
                        marketplace=target,
                        outcome="swept",
                        stage_reached=1,
                        source="cross_market",
                    )

    # An empty sweep caused by finder ERRORS is a failure (surface the reason),
    # NOT a legitimate "no matches" completion. A zero result with no errors is a
    # valid empty scan and completes normally.
    if swept == 0 and finder_errors > 0:
        raise ScanError(
            f"sweep produced 0 ASINs after {finder_errors} finder error(s). "
            f"Last error: {last_error or 'unknown'}"
        )

    out = _StageOut(
        output_count=swept,
        detail={
            "finder_calls": finder_calls,
            "finder_errors": finder_errors,
            "from_finder": swept - xm_count,
            "from_cross_market": xm_count,
        },
    )
    out.notes = notes
    return out


def _run_zombie_pass(
    conn: sqlite3.Connection,
    params: ScanParams,
    config: DeliumConfig,
    clients: ScanClients,
    as_of: date,
) -> str:
    """Small, additive zombie-listing pass for the scan's marketplaces. Returns a
    one-line summary note. Never touches the main funnel or verdicts."""
    from delium.analysis.zombies import ZombieVerdict
    from delium.discovery.zombies import ZombieClients, ZombieParams, run_zombies

    conn.commit()  # release the write lock before zombie hydrate opens its own connection
    zparams = ZombieParams(
        marketplaces=params.marketplaces,
        sweep_target=params.zombie_sweep,
        per_page=max(params.zombie_sweep, 50),
        top_n=min(10, params.zombie_sweep),
        scheduled=params.scheduled,
    )
    report = run_zombies(
        conn,
        params=zparams,
        config=config,
        clients=ZombieClients(keepa_factory=clients.keepa_factory, dfs_factory=clients.dfs_factory),
        as_of=as_of,
        confirm=None,
    )
    verified = [r for r in report.results if r.verdict is ZombieVerdict.VERIFIED]
    top = ", ".join(r.asin for r in verified[:5])
    return f"zombies: {len(verified)} verified of {len(report.results)} candidates" + (
        f" ({top})" if top else ""
    )


def _category_ids_for(profile: ResearchProfile, marketplace: str) -> tuple[list[int], str | None]:
    """The profile's numeric category ids to apply for `marketplace`, plus an
    optional note. Category ids are marketplace-specific (a US node id is
    meaningless in UK/CA), so they are applied ONLY for the marketplace(s) the
    profile declares — otherwise the filter is omitted and the reason noted."""
    ids = profile.category_ids()
    if not ids:
        return [], None
    if marketplace in profile.marketplaces:
        return ids, None
    declared = ", ".join(profile.marketplaces) or "US"
    return [], (
        f"category filter omitted for {marketplace}: the profile's category ids are "
        f"marketplace-specific (declared for {declared}) and were not resolved for {marketplace}"
    )


def _run_finder_with_fallback(
    client: Any, selection: dict[str, Any], *, mp: str, slice_id: int
) -> tuple[Any, str | None]:
    """Call the Keepa Product Finder; on a rejection (e.g. HTTP 400 from one bad
    optional filter) retry ONCE with only the core selection. Returns
    (FinderResult, note) — note is set when the fallback ran or when both attempts
    failed (finder is then None). Never raises; one bad filter can't kill a sweep."""
    from delium.providers.base import ProviderError

    try:
        return client.product_finder(selection), None
    except ProviderError as exc:
        log.warning("finder call failed (%s slice %s): %s", mp, slice_id, exc)
        core = _core_finder_selection(selection)
        if core == selection:  # nothing optional left to drop
            return None, f"finder failed ({mp} slice {slice_id}): {exc}"
        try:
            result = client.product_finder(core)
            note = f"finder retried without optional filters ({mp} slice {slice_id}) after: {exc}"
            log.info(note)
            return result, note
        except ProviderError as exc2:
            log.warning("finder fallback also failed (%s slice %s): %s", mp, slice_id, exc2)
            return None, f"finder failed even on core selection ({mp} slice {slice_id}): {exc2}"


# --- Stage 2: hydrate -------------------------------------------------------
def _stage_hydrate(
    conn: sqlite3.Connection,
    scan_id: str,
    run_id: str,
    params: ScanParams,
    config: DeliumConfig,
    clients: ScanClients,
) -> _StageOut:
    from delium.ingestion import hydrate_products

    swept = repository.get_scan_candidates(conn, scan_id, outcome="swept")
    by_mp: dict[str, list[str]] = {}
    for c in swept:
        by_mp.setdefault(c["marketplace"], []).append(c["asin"])
    conn.commit()  # release the write lock before ingestion opens its own connection
    if clients.keepa_factory is not None:
        for mp, asins in by_mp.items():
            try:
                hydrate_products(
                    asins, run_id=run_id, client=clients.keepa_factory(mp), config=config
                )
            except Exception as exc:  # noqa: BLE001 - a batch failure degrades to fewer hydrated
                log.warning("hydrate failed for %s: %s", mp, exc)

    hydrated = 0
    for c in swept:
        row = repository.get_product(conn, c["asin"], c["marketplace"])
        if row is not None and row["title"]:
            hydrated += 1
            repository.upsert_scan_candidate(
                conn,
                scan_id=scan_id,
                asin=c["asin"],
                marketplace=c["marketplace"],
                outcome="hydrated",
                stage_reached=2,
                parent_asin=_row_get(row, "parent_asin"),
            )
    return _StageOut(input_count=len(swept), output_count=hydrated)


# --- Stage 3: normalize + hard kill ----------------------------------------
def _stage_normalize_kill(
    conn: sqlite3.Connection, scan_id: str, config: DeliumConfig, profile: ResearchProfile
) -> _StageOut:
    hydrated = repository.get_scan_candidates(conn, scan_id, outcome="hydrated")
    data = load_emerging_data(config.emerging.data_version)
    # Merge variations by parent within each marketplace; keep the best-selling
    # child as the representative, mark the rest as merged-away.
    by_mp: dict[str, list[sqlite3.Row]] = {}
    for c in hydrated:
        by_mp.setdefault(c["marketplace"], []).append(c)

    kills = 0
    merged = 0
    survivors = 0
    kill_reasons: dict[str, int] = {}
    for mp, rows in by_mp.items():
        parents = {r["asin"]: (r["parent_asin"] or r["asin"]) for r in rows}
        groups = group_by_parent(
            rows,
            asin_of=lambda r: r["asin"],
            parents=parents,
            # mp is used synchronously inside group_by_parent, so the loop-var
            # binding warning is a false positive here.
            rank=lambda r: float(_monthly_sold(conn, r["asin"], mp) or 0),  # noqa: B023
        )
        for g in groups:
            rep = g.representative
            for child in g.child_asins:
                if child != rep["asin"]:
                    merged += 1
                    repository.upsert_scan_candidate(
                        conn,
                        scan_id=scan_id,
                        asin=child,
                        marketplace=mp,
                        outcome="killed",
                        stage_reached=3,
                        kill_rule="variation",
                        reason=f"variation of {g.parent_asin}",
                    )
            asin = rep["asin"]
            prod = repository.get_product(conn, asin, mp)
            established = is_established_brand(
                prod["brand"] if prod is not None else None, data.established_brands
            )
            cheap_inp, _ = build_scoring_input(conn, asin, Marketplace(mp), config, cheap_only=True)
            kill_rule: str | None = None
            if cheap_inp is not None:
                scored = score_opportunity(cheap_inp, config)
                if scored.hard_kill_triggered:
                    kill_rule = next(k.rule_id for k in scored.kills if k.kills)
            if kill_rule is not None:
                kills += 1
                kill_reasons[kill_rule] = kill_reasons.get(kill_rule, 0) + 1
                repository.upsert_scan_candidate(
                    conn,
                    scan_id=scan_id,
                    asin=asin,
                    marketplace=mp,
                    outcome="killed",
                    stage_reached=3,
                    kill_rule=kill_rule,
                    established_brand=established,
                    reason=f"hard kill {kill_rule}",
                )
            else:
                survivors += 1
                repository.upsert_scan_candidate(
                    conn,
                    scan_id=scan_id,
                    asin=asin,
                    marketplace=mp,
                    outcome="hydrated",
                    stage_reached=3,
                    established_brand=established,
                )
    return _StageOut(
        input_count=len(hydrated),
        output_count=survivors,
        killed_count=kills + merged,
        detail={"hard_kills": kills, "variations_merged": merged, "by_rule": kill_reasons},
    )


def _monthly_sold(conn: sqlite3.Connection, asin: str, marketplace: str) -> int | None:
    row = repository.get_product(conn, asin, marketplace)
    return _row_get(row, "monthly_sold") if row is not None else None


# --- Stage 4: cheap scoring -------------------------------------------------
def _stage_cheap_score(
    conn: sqlite3.Connection, scan_id: str, config: DeliumConfig, profile: ResearchProfile
) -> _StageOut:
    from datetime import date as _d

    as_of = _d.today()
    survivors = repository.get_scan_candidates(conn, scan_id, outcome="hydrated")
    scored_n = 0
    for c in survivors:
        asin, mp = c["asin"], c["marketplace"]
        hist = repository.get_price_bsr_history(conn, asin)
        price = _latest(hist, "price_cents")
        prod = repository.get_product(conn, asin, mp)
        weight = _row_get(prod, "weight_g") if prod is not None else None
        overrides = profile.profit_overrides(price_cents=price, weight_g=weight)
        inp, _ = build_scoring_input(
            conn, asin, Marketplace(mp), config, profit_overrides=overrides
        )
        if inp is None:
            continue
        scored = score_opportunity(inp, config)
        facts = {
            "category": prod["category_path"] if prod is not None else None,
            "price_cents": price,
            "monthly_sold": _row_get(prod, "monthly_sold") if prod is not None else None,
            "review_count": _latest(hist, "review_count"),
            "age_days": _age_days(hist, as_of),
            "emergence": _emergence_score(conn, asin, as_of, config),
            "review_velocity": _own_review_velocity(hist, as_of),
        }
        scored_n += 1
        repository.upsert_scan_candidate(
            conn,
            scan_id=scan_id,
            asin=asin,
            marketplace=mp,
            outcome="scored",
            stage_reached=4,
            cheap_score=scored.score,
            opportunity_score=scored.score,
            confidence=scored.confidence.level.value,
            verdict=scored.verdict.value,
            data=facts,
        )
    return _StageOut(input_count=len(survivors), output_count=scored_n)


# --- Stage 5: competitor sets ----------------------------------------------
def _stage_competitor_sets(
    conn: sqlite3.Connection,
    scan_id: str,
    run_id: str,
    params: ScanParams,
    config: DeliumConfig,
    profile: ResearchProfile,
    clients: ScanClients,
) -> _StageOut:
    from datetime import date as _d

    from delium.ingestion import fetch_keywords, hydrate_products
    from delium.providers.base import ProviderError

    as_of = _d.today()
    launch_data = load_launchability_data()
    emerging = load_emerging_data(config.emerging.data_version)
    scored = repository.get_scan_candidates(conn, scan_id, outcome="scored")
    scored.sort(
        key=lambda r: r["cheap_score"] if r["cheap_score"] is not None else -1.0, reverse=True
    )
    top = scored[: params.competitor_pool]

    built = 0
    for c in top:
        asin, mp = c["asin"], c["marketplace"]
        conn.commit()  # release the write lock before ingestion opens its own connection
        seed = _main_keyword(conn, asin, mp)
        if seed is not None and clients.dfs_factory is not None:
            try:
                fetch_keywords(seed, run_id=run_id, client=clients.dfs_factory(mp), config=config)
            except ProviderError as exc:
                log.warning("keyword fetch failed for %s: %s", asin, exc)
        comp_asins = (
            [r["asin"] for r in repository.get_serp_rankings(conn, seed, mp) if r["asin"] != asin]
            if seed is not None
            else []
        )
        if comp_asins and clients.keepa_factory is not None:
            try:
                hydrate_products(
                    comp_asins, run_id=run_id, client=clients.keepa_factory(mp), config=config
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("competitor hydrate failed for %s: %s", asin, exc)
        # Re-score opportunity now that a competitor set exists (competition pillar).
        hist = repository.get_price_bsr_history(conn, asin)
        price = _latest(hist, "price_cents")
        prod = repository.get_product(conn, asin, mp)
        weight = _row_get(prod, "weight_g") if prod is not None else None
        overrides = profile.profit_overrides(price_cents=price, weight_g=weight)
        inp, _ = build_scoring_input(
            conn, asin, Marketplace(mp), config, profit_overrides=overrides
        )
        opp = score_opportunity(inp, config).score if inp is not None else c["opportunity_score"]
        comps = [_build_competitor(conn, a, mp, as_of) for a in comp_asins]
        target_price = _target_price_cents(profile, price)
        ls = compute_launchability(
            comps,
            target_price_cents=target_price,
            thresholds=launch_data,
            established_brands=frozenset(emerging.established_brands),
        )
        facts = _loads(c["data"]) or {}
        facts.update(
            {
                "price_headroom": ls.price_headroom,
                "incumbent_freshness": ls.incumbent_freshness,
                "launchability_reason": ls.reason,
                "competitors": len(comps),
            }
        )
        built += 1
        repository.upsert_scan_candidate(
            conn,
            scan_id=scan_id,
            asin=asin,
            marketplace=mp,
            outcome="competitor_set",
            stage_reached=5,
            opportunity_score=opp,
            launchability=ls.score,
            data=facts,
        )
    return _StageOut(input_count=len(scored), output_count=built)


def _main_keyword(conn: sqlite3.Connection, asin: str, marketplace: str) -> str | None:
    phrases = repository.get_serp_keyword_phrases(conn, asin, marketplace)
    if phrases:
        return phrases[0]
    row = repository.get_product(conn, asin, marketplace)
    title = row["title"] if row is not None else None
    if not title:
        return None
    words = [w for w in title.split() if w.isalnum() or "-" in w][:4]
    return " ".join(words) or None


# --- Stage 6: rank (sellability) -------------------------------------------
def _stage_rank(
    conn: sqlite3.Connection,
    scan_id: str,
    params: ScanParams,
    config: DeliumConfig,
    profile: ResearchProfile,
) -> _StageOut:
    from datetime import date as _d

    as_of = _d.today()
    weights = load_sellability_data()
    # Everything that reached cheap scoring is rankable (kills already removed).
    survivors = [
        *repository.get_scan_candidates(conn, scan_id, outcome="competitor_set"),
        *repository.get_scan_candidates(conn, scan_id, outcome="scored"),
    ]
    ranked_rows: list[tuple[float, str, sqlite3.Row]] = []
    for c in survivors:
        asin, mp = c["asin"], c["marketplace"]
        facts = _loads(c["data"]) or {}
        hist = repository.get_price_bsr_history(conn, asin)
        momentum = product_momentum_score(_own_review_velocity(hist, as_of), weights)
        eligible = (c["verdict"] or "") != "avoid"
        si = SellabilityInput(
            opportunity_score=c["opportunity_score"],
            price_headroom=facts.get("price_headroom"),
            incumbent_freshness=facts.get("incumbent_freshness"),
            product_momentum=momentum,
            eligible=eligible,
        )
        ss = compute_sellability(si, weights)
        repository.upsert_scan_candidate(
            conn,
            scan_id=scan_id,
            asin=asin,
            marketplace=mp,
            outcome=c["outcome"],
            stage_reached=6,
            sellability=ss.score,
            confidence=ss.confidence.value,
            reason=ss.reason,
        )
        if ss.score is not None and _CONF_RANK[ss.confidence] >= _CONF_RANK[params.min_confidence]:
            ranked_rows.append((ss.score, asin, c))
    ranked_rows.sort(key=lambda t: (-t[0], t[1]))
    for i, (_score, asin, c) in enumerate(ranked_rows[: params.top_n], start=1):
        repository.upsert_scan_candidate(
            conn,
            scan_id=scan_id,
            asin=asin,
            marketplace=c["marketplace"],
            outcome="ranked",
            stage_reached=6,
            rank=i,
        )
    return _StageOut(input_count=len(survivors), output_count=min(len(ranked_rows), params.top_n))


# --- Stage 7: finalists (reviews + Claude differentiation + re-score) ------
def _stage_finalists(
    conn: sqlite3.Connection,
    scan_id: str,
    run_id: str,
    params: ScanParams,
    profile: ResearchProfile,
    config: DeliumConfig,
    clients: ScanClients,
) -> _StageOut:
    from datetime import date as _d

    as_of = _d.today()
    weights = load_sellability_data()
    ranked = repository.get_scan_candidates(conn, scan_id, outcome="ranked")
    ranked.sort(key=lambda r: r["rank"] if r["rank"] is not None else 1_000_000)
    llm_usd = 0.0
    # In light mode only the top `enrich_limit` finalists are enriched (reviews +
    # one Claude call each); the rest keep differentiation "pending". Full mode
    # enriches every finalist.
    enrich_cap = params.enrich_limit if params.light_finalists else params.top_n
    finals: list[tuple[float, str, sqlite3.Row, str]] = []
    for idx, c in enumerate(ranked[: params.top_n]):
        asin, mp = c["asin"], c["marketplace"]
        facts = _loads(c["data"]) or {}
        opp = c["opportunity_score"]
        diff_status = "pending"
        eligible = (c["verdict"] or "") != "avoid"
        if clients.enrich_finalist is not None and idx < enrich_cap:
            conn.commit()  # the enricher runs the validation pipeline on its own connection
            er = clients.enrich_finalist(asin, mp, run_id)
            llm_usd += er.llm_usd
            if er.opportunity_score is not None:
                opp = er.opportunity_score
            diff_status = "done" if er.differentiation_available else "pending"
            eligible = eligible and er.eligible
        hist = repository.get_price_bsr_history(conn, asin)
        momentum = product_momentum_score(_own_review_velocity(hist, as_of), weights)
        ss = compute_sellability(
            SellabilityInput(
                opportunity_score=opp,
                price_headroom=facts.get("price_headroom"),
                incumbent_freshness=facts.get("incumbent_freshness"),
                product_momentum=momentum,
                eligible=eligible,
            ),
            weights,
        )
        finals.append((ss.score if ss.score is not None else -1.0, asin, c, diff_status))

    finals.sort(key=lambda t: (-t[0], t[1]))
    for i, (score, asin, c, diff_status) in enumerate(finals, start=1):
        ss_score = score if score >= 0 else None
        repository.upsert_scan_candidate(
            conn,
            scan_id=scan_id,
            asin=asin,
            marketplace=c["marketplace"],
            outcome="finalist",
            stage_reached=7,
            rank=i,
            sellability=ss_score,
            opportunity_score=c["opportunity_score"],
            differentiation_status=diff_status,
        )
    return _StageOut(input_count=len(ranked), output_count=len(finals), llm_usd=round(llm_usd, 4))


# --- Stage 8: report --------------------------------------------------------
def _stage_report(conn: sqlite3.Connection, scan_id: str, config: DeliumConfig) -> _StageOut:
    momentum_data = load_momentum_data()
    scored = [
        *repository.get_scan_candidates(conn, scan_id, outcome="finalist"),
        *repository.get_scan_candidates(conn, scan_id, outcome="ranked"),
        *repository.get_scan_candidates(conn, scan_id, outcome="competitor_set"),
        *repository.get_scan_candidates(conn, scan_id, outcome="scored"),
    ]
    seen: set[str] = set()
    products: list[MomentumProduct] = []
    for c in scored:
        if c["asin"] in seen:
            continue
        seen.add(c["asin"])
        f = _loads(c["data"]) or {}
        products.append(
            MomentumProduct(
                category=f.get("category"),
                emergence_score=f.get("emergence"),
                age_days=f.get("age_days"),
                review_count=f.get("review_count"),
                monthly_sold=f.get("monthly_sold"),
                price_cents=f.get("price_cents"),
            )
        )
    cats = compute_category_momentum(products, momentum_data, top_n=8)
    for cm in cats:
        repository.upsert_scan_category(
            conn,
            scan_id=scan_id,
            category=cm.category,
            momentum_score=cm.score,
            metrics=cm.metrics.__dict__,
            reason=cm.reason,
        )
    return _StageOut(input_count=len(products), output_count=len(cats))


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------
def build_report(conn: sqlite3.Connection, scan_id: str) -> ScanReport:
    scan = repository.get_scan(conn, scan_id)
    if scan is None:
        raise ValueError(f"no scan {scan_id!r}")
    stages = repository.get_scan_stages(conn, scan_id)
    inbox = repository.scan_inbox(conn, scan_id)
    cats = repository.get_scan_categories(conn, scan_id)
    all_c = repository.get_scan_candidates(conn, scan_id)
    funnel = {
        "swept": sum(1 for c in all_c if c["stage_reached"] >= 1),
        "hydrated": sum(1 for c in all_c if c["stage_reached"] >= 2 and c["outcome"] != "killed"),
        "killed": sum(1 for c in all_c if c["outcome"] == "killed"),
        "scored": sum(1 for c in all_c if c["cheap_score"] is not None),
        "competitor_set": sum(1 for c in all_c if c["launchability"] is not None),
        "finalists": len(inbox),
    }
    return ScanReport(
        scan_id=scan_id,
        status=scan["status"],
        marketplaces=tuple(_loads(scan["marketplaces"]) or []),
        funnel=funnel,
        stages=[
            {
                "stage": s["stage"],
                "name": s["name"],
                "status": s["status"],
                "in": s["input_count"],
                "out": s["output_count"],
                "killed": s["killed_count"],
                "tokens": s["keepa_tokens"],
                "data_usd": round(s["data_usd"], 4),
                "llm_usd": round(s["llm_usd"], 4),
            }
            for s in stages
        ],
        finalists=[
            {
                "rank": c["rank"],
                "asin": c["asin"],
                "marketplace": c["marketplace"],
                "sellability": c["sellability"],
                "opportunity": c["opportunity_score"],
                "confidence": c["confidence"],
                "differentiation": c["differentiation_status"],
                "reason": c["reason"],
            }
            for c in inbox
        ],
        categories=[
            {"category": c["category"], "score": c["momentum_score"], "reason": c["reason"]}
            for c in cats
        ],
        notes=list(_loads(scan["notes"]) or []),
        keepa_tokens=int(scan["keepa_tokens"]),
        data_usd=round(float(scan["data_usd"]), 4),
        llm_usd=round(float(scan["llm_usd"]), 4),
    )
