"""Delium local web UI (Streamlit) — runs on localhost only, single user.

Thin presentation layer over `delium.ui.services` (which calls the same internal
functions as the CLI) and `delium.ui.format` / `.costs` / `.credentials`. Launch
with `delium ui`. This module is intentionally excluded from unit tests and
coverage; its logic lives in the tested helper modules.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from delium.config import ConfigError, load_config
from delium.database.connection import get_connection
from delium.providers.base import ProviderError
from delium.ui import costs, credentials, format, services

_MARKETPLACES = ["US", "UK", "CA", "AU", "IN"]


def _config() -> Any:
    return load_config()


_usd = format.usd_md
_escape_money = format.escape_money


# ---------------------------------------------------------------------------
# Shared widgets
# ---------------------------------------------------------------------------
def _sidebar_credentials() -> None:
    st.sidebar.markdown("### Providers")
    for p in credentials.provider_status():
        icon = "✅" if p.configured else "⬜"
        st.sidebar.markdown(f"{icon} **{p.label}**")
        st.sidebar.caption(("configured — " if p.configured else "missing — ") + p.enables)
    st.sidebar.caption("Credentials are read from the environment / .env — values are never shown.")


def _cost_gate(estimate: costs.CostEstimate, action_label: str) -> bool:
    """Render the pre-flight cost panel and return True only when the user has
    confirmed. Cached actions are free and need a single click."""
    if estimate.fully_cached:
        st.success(f"Fully cached — this will cost **{_usd(0)}** (no provider call).")
        return bool(st.button(f"Run {action_label} (cached)"))
    providers = ", ".join(estimate.providers) or "no providers"
    st.warning(
        f"This will call: **{providers}**.  Estimated cost: "
        f"**{_usd(estimate.est_low_usd)}–{_usd(estimate.est_high_usd)}**."
    )
    for note in estimate.notes:
        st.caption("• " + _escape_money(note))
    return bool(st.button(f"Confirm & run {action_label}", type="primary"))


def _actual_cost(data_usd: float, llm_usd: float = 0.0, *, from_cache: bool | None = None) -> None:
    if from_cache:
        st.caption(f"Served from cache — actual cost {_usd(0)}.")
        return
    total = data_usd + llm_usd
    st.caption(
        f"Actual cost: **{_usd(total, places=4)}** "
        f"(data {_usd(data_usd, places=4)} + LLM {_usd(llm_usd, places=4)})."
    )


def _verdict_banner(verdict_value: str, subtitle: str) -> None:
    label, color = format.verdict_style(verdict_value)
    st.markdown(
        f"<div style='padding:14px 18px;border-radius:10px;background:{color};"
        f"color:white;font-weight:700;font-size:1.4rem;'>{label}"
        f"<div style='font-size:0.85rem;font-weight:400;opacity:0.9'>{subtitle}</div></div>",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Navigation + profile helpers
# ---------------------------------------------------------------------------
def _active_profile() -> Any:
    """The active Research Profile (cached in session state for one rerun)."""
    if "_profile" not in st.session_state:
        st.session_state["_profile"] = services.active_profile()
    return st.session_state["_profile"]


def _refresh_profile() -> None:
    st.session_state.pop("_profile", None)


def _profile_banner() -> None:
    try:
        profile = _active_profile()
    except Exception:  # noqa: BLE001 - never let the banner break a page
        return
    st.caption(
        f"🧭 **Profile: {profile.name}** · risk {profile.risk_tolerance.value} · "
        f"budget {_usd(profile.budget_usd or 0, places=0)} · "
        f"price {_usd((profile.price_min_cents or 0) / 100, places=0)}–"
        f"{_usd((profile.price_max_cents or 0) / 100, places=0)}"
    )


def _profile_marketplace() -> str:
    mps = _active_profile().marketplaces or ("US",)
    return mps[0] if mps[0] in _MARKETPLACES else "US"


def _goto(section: str, **state: Any) -> None:
    """Switch the top-level section (and stash any state) then rerun."""
    st.session_state.update(state)
    st.session_state["_section"] = section
    st.rerun()


def _open_workspace_picker(asins: list[str], marketplace: str, *, key: str) -> None:
    """Offer to open one of a result's ASINs in the Product workspace."""
    asins = [a for a in dict.fromkeys(asins) if a]
    if not asins:
        return
    col1, col2 = st.columns([3, 1])
    choice = col1.selectbox("Open a product in the workspace", asins, key=f"{key}_pick")
    if col2.button("Open →", key=f"{key}_open"):
        _goto("Product", ws_asin=choice, ws_mp=marketplace)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def page_keyword_research() -> None:
    st.header("Keyword research")
    st.caption("Search volume, related keywords, and top Amazon SERP ASINs (DataForSEO).")
    if not credentials.is_configured("dataforseo"):
        st.info(
            "DataForSEO credentials are not configured — set DELIUM_DATAFORSEO_LOGIN / "
            "DELIUM_DATAFORSEO_PASSWORD to use this page."
        )
        return

    seed = st.text_input("Seed keyword", placeholder="silicone baby food tray")
    col1, col2 = st.columns(2)
    marketplace = col1.selectbox(
        "Marketplace", _MARKETPLACES, index=_MARKETPLACES.index(_profile_marketplace()), key="kw_mp"
    )
    force = col2.checkbox("Bypass cache (refetch)", key="kw_force")
    if not seed.strip():
        return

    config = _config()
    with get_connection() as conn:
        estimate = costs.keyword_estimate(conn, marketplace, seed, config, force=force)
    if not _cost_gate(estimate, "keyword research"):
        return
    try:
        with st.spinner("Fetching keyword data…"):
            result = services.keyword_research(seed, marketplace, config, force=force)
    except (ProviderError, ConfigError) as exc:
        st.error(f"Fetch failed: {exc}")
        return

    _actual_cost(result.cost_usd, from_cache=result.from_cache)
    vol = f"{result.seed_volume:,}" if result.seed_volume is not None else "—"
    st.metric(f"Search volume · {result.seed}", vol)
    st.subheader("Related keywords")
    st.dataframe(format.related_rows(result), width="stretch", hide_index=True)
    st.subheader("Top SERP ASINs")
    serp = format.serp_rows(result)
    st.dataframe(serp, width="stretch", hide_index=True)
    _open_workspace_picker([r["asin"] for r in serp], marketplace, key="kw")


def page_product_lookup() -> None:
    st.header("Product lookup")
    st.caption("Product facts and price/BSR history (Keepa).")
    if not credentials.is_configured("keepa"):
        st.info("Keepa is not configured — set DELIUM_KEEPA_API_KEY to use this page.")
        return

    asin = st.text_input("ASIN", placeholder="B0XXXXXXXX")
    col1, col2 = st.columns(2)
    marketplace = col1.selectbox("Marketplace", _MARKETPLACES, key="pl_mp")
    force = col2.checkbox("Bypass cache (refetch)", key="pl_force")
    if not asin.strip():
        return

    config = _config()
    with get_connection() as conn:
        estimate = costs.product_estimate(conn, marketplace, asin.strip(), config, force=force)
    if not _cost_gate(estimate, "product lookup"):
        return
    try:
        with st.spinner("Fetching product…"):
            lookup = services.product_lookup(asin.strip(), marketplace, config, force=force)
    except (ProviderError, ConfigError) as exc:
        st.error(f"Fetch failed: {exc}")
        return

    view = lookup.view
    _actual_cost(view.cost_usd, from_cache=view.from_cache)
    if not view.found:
        st.warning(f"ASIN {view.asin} not found on Keepa for {view.marketplace}.")
        return

    st.subheader(view.title or view.asin)
    facts = {
        "ASIN": view.asin,
        "Brand": view.brand or "—",
        "Category": view.category_path or "—",
        "Weight (g)": view.weight_g or "—",
        "Images": view.images_count if view.images_count is not None else "—",
        "Latest price": f"${view.latest_price_cents / 100:.2f}" if view.latest_price_cents else "—",
        "Latest BSR": view.latest_bsr if view.latest_bsr is not None else "—",
        "History points": view.history_points,
    }
    st.table({"field": list(facts), "value": [str(v) for v in facts.values()]})

    series = format.history_series(lookup.history)
    if series["date"]:
        frame = pd.DataFrame(series).set_index("date")
        st.subheader("Price history (USD)")
        st.line_chart(frame[["price_usd"]])
        st.subheader("BSR history (lower = better)")
        st.line_chart(frame[["bsr"]])


def page_validate() -> None:
    st.header("Validate")
    st.caption("Full deterministic validation → Buy / Test / Avoid. scoring.py owns the verdict.")
    if not credentials.is_configured("keepa"):
        st.info(
            "Validate needs Keepa for product data — set DELIUM_KEEPA_API_KEY. "
            "(DataForSEO/reviews/LLM are optional and the run degrades cleanly without them.)"
        )
        return

    target = st.text_input("ASIN or keyword", placeholder="B0XXXXXXXX or 'silicone baby food tray'")
    col1, col2, col3 = st.columns(3)
    marketplace = col1.selectbox("Marketplace", _MARKETPLACES, key="val_mp")
    cogs = col2.number_input("COGS override (USD)", min_value=0.0, value=0.0, step=0.5) or None
    freight = (
        col3.number_input("Freight override (USD)", min_value=0.0, value=0.0, step=0.1) or None
    )
    force = st.checkbox("Bypass cache (refetch)", key="val_force")
    if not target.strip():
        return

    config = _config()
    estimate = costs.validate_estimate(
        config,
        keepa=credentials.is_configured("keepa"),
        dataforseo=credentials.is_configured("dataforseo"),
        reviews=credentials.is_configured("reviews"),
        llm=credentials.is_configured("llm"),
        force=force,
    )
    if not _cost_gate(estimate, "validation"):
        return
    try:
        with st.spinner("Running validation…"):
            res = services.validate(
                target.strip(),
                marketplace,
                config,
                cogs=cogs,
                freight=freight,
                force=force,
            )
    except (ProviderError, ConfigError) as exc:
        st.error(f"Validation failed: {exc}")
        return

    report = res.report
    _actual_cost(report.data_cost_usd, report.llm_cost_usd)
    st.caption(f"Keepa tokens used this run: {res.tokens_used}")
    scored = report.scored
    if scored is None:
        st.warning(f"No verdict produced — status: `{report.status.value}`.")
        for note in report.notes:
            st.caption(f"• {note}")
        return

    suff = "insufficient data" if scored.insufficient_data else "sufficient data"
    _verdict_banner(
        scored.verdict.value,
        f"score {scored.score:.0f}/100 · {scored.confidence.level.value} confidence · {suff}",
    )
    if scored.strategist_pending:
        st.caption("A Buy here would be provisional pending Strategist (G5) concurrence.")

    st.subheader("Pillars")
    st.dataframe(format.pillar_rows(scored), width="stretch", hide_index=True)
    kills = format.kill_rows(scored)
    if kills:
        st.subheader("Hard kills / borderline")
        st.dataframe(kills, width="stretch", hide_index=True)
    st.subheader("Gates")
    st.dataframe(format.gate_rows(scored), width="stretch", hide_index=True)

    with st.expander("Full report (Markdown)", expanded=False):
        st.markdown(res.markdown)


def page_discover() -> None:
    st.header("Discover")
    st.caption("Cheap, wide triage from seed keyword(s). A research queue — never a Buy verdict.")
    if not credentials.is_configured("dataforseo"):
        st.info("Discovery needs DataForSEO for keyword/SERP expansion — set DELIUM_DATAFORSEO_*.")
        return
    if not credentials.is_configured("keepa"):
        st.warning(
            "Keepa is not configured: candidates will be surfaced but cannot be scored "
            "(they will show as 'needs data'). Add DELIUM_KEEPA_API_KEY to score them."
        )

    raw = st.text_area("Seed keyword(s), one per line", placeholder="silicone baby food tray")
    col1, col2 = st.columns(2)
    marketplace = col1.selectbox(
        "Marketplace",
        _MARKETPLACES,
        index=_MARKETPLACES.index(_profile_marketplace()),
        key="disc_mp",
    )
    force = col2.checkbox("Bypass cache (refetch)", key="disc_force")
    keywords = [line.strip() for line in raw.splitlines() if line.strip()]
    if not keywords:
        return

    config = _config()
    estimate = costs.discover_estimate(
        config,
        keyword_count=len(keywords),
        dataforseo=credentials.is_configured("dataforseo"),
        keepa=credentials.is_configured("keepa"),
    )
    if not _cost_gate(estimate, "discovery"):
        return
    try:
        with st.spinner("Discovering candidates…"):
            res = services.discover(keywords, marketplace, config, force=force)
    except (ProviderError, ConfigError) as exc:
        st.error(f"Discovery failed: {exc}")
        return

    report = res.report
    st.caption(
        f"discovered {report.discovered_count} · ranked {len(report.ranked)} · "
        f"killed {len(report.killed)} · unresolved {len(report.unresolved)} · "
        f"Keepa tokens used: {res.tokens_used}"
    )
    ranked_asins = [ec.asin for ec in report.ranked if ec.scored is not None]
    parents = services.parent_map(ranked_asins, marketplace)
    rows = format.discovery_rows(report, parents)
    if rows:
        st.subheader("Ranked candidates (variations collapsed by parent)")
        st.dataframe(rows, width="stretch", hide_index=True)
        _open_workspace_picker(ranked_asins, marketplace, key="disc")

    if report.killed:
        st.subheader(f"Eliminated by hard kills ({len(report.killed)})")
        st.caption(
            "Every killed candidate with the exact rule(s) and observed vs. threshold value."
        )
        facts = services.discovery_product_facts(report)
        st.dataframe(format.discovery_killed_rows(report, facts), width="stretch", hide_index=True)

    if not rows:
        # Explain WHY nothing was ranked, precisely — the old message wrongly
        # blamed missing Keepa even when candidates were hydrated and then killed.
        if not credentials.is_configured("keepa"):
            st.info(
                "No candidates were scored because Keepa is not configured — they were "
                "surfaced but could not be hydrated. Add DELIUM_KEEPA_API_KEY to score them."
            )
        elif report.killed:
            st.info(
                "Every candidate was eliminated by a hard kill — see the table above for the "
                "exact rule and value that killed each one."
            )
        elif report.unresolved:
            st.info(
                "Candidates were surfaced but could not be hydrated (no product data yet). "
                "Try again, or check Keepa token availability on the Usage page."
            )
        else:
            st.info("No candidates were surfaced for these seed keyword(s).")


def page_emerging() -> None:
    st.header("Emerging products")
    st.caption(
        "Recently-launched products already gaining traction while competition is weak "
        "(Keepa Product Finder), scored through the existing pipeline. Emergence explains "
        "WHY; scoring.py still owns Buy/Test/Avoid."
    )
    if not credentials.is_configured("keepa"):
        st.info(
            "Emerging search needs Keepa (the Product Finder is Keepa-only). "
            "Set DELIUM_KEEPA_API_KEY."
        )
        return

    config = _config()
    profile = _active_profile()
    col1, col2 = st.columns(2)
    marketplace = col1.selectbox(
        "Marketplace", _MARKETPLACES, index=_MARKETPLACES.index(_profile_marketplace()), key="em_mp"
    )
    cats_raw = col2.text_input(
        "Keepa category ids (comma-separated, optional)",
        value=",".join(str(c) for c in profile.category_ids()),
        key="em_cats",
    )
    col3, col4 = st.columns(2)
    max_reviews = col3.number_input(
        "Max reviews override", min_value=0, value=0, step=10, key="em_rev"
    )
    max_age = col4.number_input(
        "Max age days override", min_value=0, value=0, step=30, key="em_age"
    )
    category_ids = [int(x) for x in cats_raw.replace(" ", "").split(",") if x.strip().isdigit()]
    # Profile drives the finder defaults (price band, review ceiling); explicit
    # per-run overrides below win.
    overrides: dict[str, int] = dict(profile.finder_overrides())
    if max_reviews:
        overrides["reviews_max"] = int(max_reviews)
    if max_age:
        overrides["age_max_days"] = int(max_age)
    st.caption(f"Finder defaults from profile: {overrides or 'none'}")

    est, finder_tok, product_tok = costs.emerging_estimate(
        config, dataforseo=credentials.is_configured("dataforseo")
    )
    st.warning(
        f"This calls **{', '.join(est.providers)}**. Estimated Keepa tokens: "
        f"~**{finder_tok}** (Product Finder) + up to **{product_tok}** (product hydration, "
        "worst case — cache is reused)."
    )
    for note in est.notes:
        st.caption("• " + _escape_money(note))
    if not st.button("Confirm & run emerging search", type="primary"):
        return
    try:
        with st.spinner("Searching for emerging products…"):
            report = services.emerging(marketplace, category_ids, config, overrides=overrides)
    except (ProviderError, ConfigError) as exc:
        st.error(f"Emerging search failed: {exc}")
        return

    for note in report.notes:
        st.info(note)
    st.caption(
        f"Product Finder matched {report.finder_total_results or 0} total · "
        f"tokens used: {report.finder_tokens} finder + {report.product_tokens} product."
    )
    if report.ranked:
        facts, parents = services.emerging_facts(report)
        st.subheader("Emerging candidates (one row per parent listing)")
        st.caption(
            "Variations are collapsed to one row per parent (variation count shown). "
            "A blank pillar column means that pillar is *unknown* (no data yet) — it "
            "lowers confidence, it is not scored as 0."
        )
        st.dataframe(
            format.emerging_rich_rows(list(report.ranked), facts, parents),
            width="stretch",
            hide_index=True,
        )
        _open_workspace_picker([c.asin for c in report.ranked], marketplace, key="em")
        _emerging_cards(report, facts)
    else:
        st.info("No emerging candidates survived scoring.")
    killed = format.emerging_killed_rows(list(report.killed))
    if killed:
        st.subheader("Emerging but hard-killed (excluded, reasons shown)")
        st.dataframe(killed, width="stretch", hide_index=True)


def _emerging_cards(report: Any, facts: dict[str, Any]) -> None:
    """Deterministic plain-English card per ranked candidate (no LLM)."""
    from delium.discovery.diagnostics import diagnose_scored
    from delium.reports.cards import CardFacts, build_card

    st.subheader("What each result means")
    for c in report.ranked:
        s = c.evaluated.scored
        if s is None:
            continue
        diag = diagnose_scored(c.asin, report.marketplace.value, s)
        f = facts.get(c.asin, {})
        card = build_card(
            diag,
            CardFacts(
                title=f.get("title"),
                brand=f.get("brand"),
                category=f.get("category"),
                price_cents=f.get("price_cents"),
                bsr=f.get("bsr"),
                reviews=f.get("reviews"),
                age_days=c.emergence.age_days,
                monthly_units=f.get("monthly_units"),
                emergence=c.emergence.emergence_score,
                established_brand=getattr(c, "established_brand", False),
            ),
        )
        with st.expander(_escape_money(card.headline)):
            for label, items in (
                ("Why it looks promising", card.promising),
                ("Why confidence is low", card.low_confidence),
                ("What would raise it", card.to_raise),
            ):
                if items:
                    st.markdown(f"**{label}:**")
                    for it in items:
                        st.markdown("- " + _escape_money(it))
            for flag in card.flags:
                st.warning(_escape_money(flag))
            st.markdown("**Next:** " + _escape_money(card.next_action))


def page_cross_market() -> None:
    st.header("Cross-market")
    st.caption(
        "Products proven in a source marketplace that look underpenetrated in a target. "
        "Reads already-fetched data — a discovery signal, not a verdict."
    )
    col1, col2 = st.columns(2)
    source = col1.selectbox("Source marketplace", _MARKETPLACES, key="cm_src")
    targets = col2.multiselect(
        "Target marketplace(s)", [m for m in _MARKETPLACES], default=[], key="cm_tgt"
    )
    targets = [t for t in targets if t != source]
    if not targets:
        st.info("Pick at least one target marketplace different from the source.")
        return

    st.caption("This reads cached data only — no paid API calls.")
    if not st.button("Run cross-market", type="primary"):
        return
    config = _config()
    try:
        candidates = services.cross_market(source, targets, config)
    except (ProviderError, ConfigError) as exc:
        st.error(f"Cross-market failed: {exc}")
        return
    parents = services.parent_map([c.source_asin for c in candidates], source)
    rows = format.cross_market_rows(candidates, parents)
    if rows:
        st.dataframe(rows, width="stretch", hide_index=True)
    else:
        st.info(
            "No qualifying candidates. Fetch source products/keywords first "
            "(Product lookup / Keyword research with the source marketplace)."
        )


def page_history() -> None:
    st.header("History")
    st.caption("Past runs, validations, and saved reports from the local database — no API calls.")

    st.subheader("Recent validations")
    vals = format.validation_rows(services.recent_validations())
    if vals:
        st.dataframe(vals, width="stretch", hide_index=True)
    else:
        st.caption("none yet")

    st.subheader("Emerging runs")
    em_runs = services.recent_emerging_runs()
    if em_runs:
        labels = [
            f"{r['created_at']} · {r['marketplace']} · {r['page_size']} scanned" for r in em_runs
        ]
        idx = st.selectbox("Emerging run", range(len(em_runs)), format_func=lambda i: labels[i])
        cands = format.emerging_candidate_rows(services.emerging_candidates(em_runs[idx]["run_id"]))
        st.dataframe(cands, width="stretch", hide_index=True) if cands else st.caption(
            "no candidates"
        )
    else:
        st.caption("none yet")

    st.subheader("Recent runs")
    runs = format.run_rows(services.recent_runs())
    if runs:
        st.dataframe(runs, width="stretch", hide_index=True)
    else:
        st.caption("none yet")

    st.subheader("Saved reports")
    files = services.report_files()
    if not files:
        st.caption("none yet")
        return
    labels = [f"{f['modified']} · {f['name']}" for f in files]
    choice = st.selectbox("Report", range(len(files)), format_func=lambda i: labels[i])
    st.markdown(services.read_report(files[choice]["path"]))


def page_usage() -> None:
    st.header("Usage")
    st.caption(
        "Keepa token balance, and spend / tokens per provider and per run type — "
        "all from the local database and a free Keepa token check. API keys are never shown."
    )

    st.subheader("Keepa tokens")
    if not credentials.is_configured("keepa"):
        st.info("Keepa is not configured — set DELIUM_KEEPA_API_KEY to see the live token balance.")
    else:
        status = services.keepa_token_status()
        if status is None:
            st.warning("Could not read the Keepa token balance right now.")
        else:
            c1, c2 = st.columns(2)
            left = "—" if status.tokens_left is None else f"{status.tokens_left:,}"
            c1.metric("Tokens left", left)
            c2.metric(
                "Refill rate",
                "—" if status.refill_rate is None else f"{status.refill_rate:,}/min",
            )
            st.caption("Live from Keepa's `/token` endpoint (costs 0 tokens).")

    st.subheader("Spend & tokens per provider (last 30 days)")
    spend = format.spend_rows(services.spend_by_provider_day(days=30))
    if spend:
        st.dataframe(spend, width="stretch", hide_index=True)
    else:
        st.caption("no fetches recorded yet")

    st.subheader("Cost & tokens per run type")
    st.caption(
        "Average data spend, LLM spend, and Keepa tokens per validate / discover / emerging run."
    )
    summary = format.run_type_rows(services.run_type_summary())
    if summary:
        st.dataframe(summary, width="stretch", hide_index=True)
    else:
        st.caption("no runs recorded yet")


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------
def page_home() -> None:
    st.header("Home")
    config = _config()
    summary = services.home_summary(config)

    tokens = summary["keepa_tokens"]
    if tokens is not None and tokens.tokens_left is not None:
        st.metric("Keepa tokens left", f"{tokens.tokens_left:,}")

    st.subheader("Shortlist")
    shortlist = summary["shortlist"]
    if shortlist:
        st.dataframe(
            [
                {
                    "asin": r["asin"],
                    "marketplace": r["marketplace"],
                    "status": r["status"],
                    "notes": r["notes"] or "",
                    "updated": r["updated_at"],
                }
                for r in shortlist
            ],
            width="stretch",
            hide_index=True,
        )
        _open_workspace_picker([r["asin"] for r in shortlist], "US", key="home_short")
    else:
        st.caption("Nothing shortlisted yet — open a product and add it.")

    st.subheader("Top opportunities matching your profile")
    top = summary["top_opportunities"]
    if top:
        st.dataframe(format.validation_rows(top), width="stretch", hide_index=True)
        _open_workspace_picker([r["asin"] for r in top], "US", key="home_top")
    else:
        st.caption("No validated candidates yet — run a Find or Validate.")

    st.subheader("Recent runs")
    runs = format.run_rows(summary["runs"])
    st.dataframe(runs, width="stretch", hide_index=True) if runs else st.caption("none yet")

    st.divider()
    if st.button("Open a product by ASIN →"):
        _goto("Product")


# ---------------------------------------------------------------------------
# Find (Emerging / Keyword / Discover / Cross-market / History)
# ---------------------------------------------------------------------------
_FIND_MODES = {
    "Emerging": page_emerging,
    "Keyword": page_keyword_research,
    "Black Box (Discover)": page_discover,
    "Cross-market": page_cross_market,
    "History": page_history,
}


def page_find() -> None:
    st.header("Find")
    mode = st.radio("Mode", list(_FIND_MODES), horizontal=True, key="find_mode")
    st.divider()
    _FIND_MODES[mode]()


# ---------------------------------------------------------------------------
# Product workspace
# ---------------------------------------------------------------------------
def page_product() -> None:
    st.header("Product workspace")
    config = _config()
    default_asin = str(st.session_state.get("ws_asin", ""))
    default_mp = str(st.session_state.get("ws_mp", _profile_marketplace()))
    col1, col2 = st.columns([3, 1])
    asin = col1.text_input("ASIN", value=default_asin, placeholder="B0XXXXXXXX", key="ws_asin_in")
    marketplace = col2.selectbox(
        "Marketplace",
        _MARKETPLACES,
        index=_MARKETPLACES.index(default_mp if default_mp in _MARKETPLACES else "US"),
    )
    if not asin.strip():
        st.info("Enter an ASIN, or open one from a Find result / the shortlist.")
        return

    ws = services.workspace(asin, marketplace, config)
    if not ws.found:
        st.warning(
            f"{ws.asin} has not been fetched in {marketplace} yet. Run a Deep dive below to "
            "fetch it (Keepa)."
        )
    _workspace_actions(ws, marketplace, config)

    tabs = st.tabs(
        [
            "Overview",
            "Sales & momentum",
            "Keywords",
            "Competitors",
            "Reviews & ideas",
            "Profit",
            "Risk & verdict",
        ]
    )
    with tabs[0]:
        _ws_overview(ws)
    with tabs[1]:
        _ws_momentum(ws)
    with tabs[2]:
        _ws_keywords(ws)
    with tabs[3]:
        _ws_competitors(ws)
    with tabs[4]:
        _ws_reviews(ws)
    with tabs[5]:
        _ws_profit(ws)
    with tabs[6]:
        _ws_risk(ws)


def _workspace_actions(ws: Any, marketplace: str, config: Any) -> None:
    """Shortlist controls, re-check, and the single Deep-dive button."""
    c1, c2, c3 = st.columns(3)
    statuses = ["researching", "sampling", "rejected", "launched"]
    with c1:
        cur = ws.shortlist.status or "researching"
        status = st.selectbox("Shortlist status", statuses, index=statuses.index(cur))
        notes = st.text_input("Notes", value=ws.shortlist.notes or "")
        if ws.shortlist.on_shortlist:
            if st.button("Update shortlist"):
                services.set_shortlist(ws.asin, marketplace, status=status, notes=notes)
                _goto("Product", ws_asin=ws.asin, ws_mp=marketplace)
            if st.button("Remove from shortlist"):
                services.remove_from_shortlist(ws.asin, marketplace)
                _goto("Product", ws_asin=ws.asin, ws_mp=marketplace)
        elif st.button("Add to shortlist"):
            services.set_shortlist(ws.asin, marketplace, status=status, notes=notes)
            _goto("Product", ws_asin=ws.asin, ws_mp=marketplace)
    with c2:
        st.caption("Re-check refreshes Keepa data and stores a snapshot to compare momentum.")
        if credentials.is_configured("keepa") and st.button("Re-check now (Keepa)"):
            with st.spinner("Refreshing Keepa data…"):
                _ws, tokens = services.recheck(ws.asin, marketplace, config)
            st.success(f"Re-checked — {tokens} Keepa tokens used.")
            _goto("Product", ws_asin=ws.asin, ws_mp=marketplace)
    with c3:
        _deep_dive_control(ws, marketplace, config)


def _deep_dive_control(ws: Any, marketplace: str, config: Any) -> None:
    plan = services.deep_dive_plan(ws, config)
    if not plan.has_work:
        st.caption("Deep dive: nothing missing (or no providers configured).")
        return
    st.markdown("**Deep dive** — fetch what's missing:")
    for step in plan.steps:
        st.caption(f"• {step.label} — {_escape_money(step.reason)}")
    st.caption(
        f"Estimated: {_usd(plan.total_cost_usd)} + ~{plan.total_keepa_tokens} Keepa tokens "
        "(cache-first)."
    )
    if st.button("Confirm & run Deep dive", type="primary"):
        with st.spinner("Fetching missing evidence…"):
            _ws, tokens = services.run_deep_dive(ws.asin, marketplace, config, plan)
        st.success(f"Deep dive complete — {tokens} Keepa tokens used.")
        _goto("Product", ws_asin=ws.asin, ws_mp=marketplace)


def _ws_overview(ws: Any) -> None:
    if ws.card is not None:
        st.markdown(f"### {_escape_money(ws.card.headline)}")
        for label, items in (
            ("Why it looks promising", ws.card.promising),
            ("Why confidence is uncertain", ws.card.low_confidence),
            ("What would raise confidence", ws.card.to_raise),
        ):
            if items:
                st.markdown(f"**{label}:**")
                for it in items:
                    st.markdown("- " + _escape_money(it))
        for flag in ws.card.flags:
            st.warning(_escape_money(flag))
        st.markdown("**Next action:** " + _escape_money(ws.card.next_action))
    else:
        st.info("Not enough data to score yet. Run a Deep dive to fetch this product.")
    f = ws.facts
    st.divider()
    st.markdown(f"**{f.title or ws.asin}** · {f.brand or '—'} · {f.category or '—'}")
    if ws.variation.parent_asin:
        st.caption(
            f"Variation group: parent {ws.variation.parent_asin} · "
            f"{ws.variation.variation_count} variations."
        )


def _ws_momentum(ws: Any) -> None:
    m = ws.momentum
    if m is None or not m.dates:
        st.info("No price/BSR history yet.")
        return
    c1, c2, c3 = st.columns(3)
    c1.metric(
        "Keepa monthly sold", "—" if m.keepa_monthly_sold is None else f"{m.keepa_monthly_sold:,}"
    )
    c2.metric(
        "Delium est. units/mo",
        "—" if m.delium_units_estimate is None else f"{m.delium_units_estimate:,}",
    )
    c3.metric("Launch age (days)", "—" if m.age_days is None else f"{m.age_days:,}")
    frame = pd.DataFrame(
        {"date": m.dates, "price_usd": m.price_usd, "bsr": m.bsr, "reviews": m.reviews}
    ).set_index("date")
    st.line_chart(frame[["price_usd"]])
    st.line_chart(frame[["bsr"]])
    st.line_chart(frame[["reviews"]])
    if ws.snapshots:
        st.subheader("Re-check history (momentum over time)")
        st.dataframe(
            [
                {
                    "captured": s["captured_at"],
                    "bsr": s["bsr"],
                    "monthly_sold": s["monthly_sold"],
                    "verdict": s["verdict"],
                }
                for s in ws.snapshots
            ],
            width="stretch",
            hide_index=True,
        )


def _ws_keywords(ws: Any) -> None:
    kw = ws.keywords
    if kw.keywords:
        st.dataframe(
            [
                {"keyword": k.phrase, "volume": k.volume, "primary": k.is_primary}
                for k in kw.keywords
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.caption("No keywords resolved yet.")
    st.info(kw.note)


def _ws_competitors(ws: Any) -> None:
    cs = ws.competitors
    if cs.launchability_pct is not None:
        st.metric("Launchability", f"{cs.launchability_pct:.0f}%")
    st.caption(cs.note)
    if cs.competitors:
        st.dataframe(
            [
                {
                    "asin": c.asin,
                    "brand": c.brand,
                    "price_usd": None if c.price_cents is None else round(c.price_cents / 100, 2),
                    "reviews": c.reviews,
                    "rating": c.rating,
                    "bsr": c.bsr,
                    "monthly_sold": c.monthly_sold,
                    "age_days": c.age_days,
                    "variations": c.variation_count,
                }
                for c in cs.competitors
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.caption("No competitor set yet — run a Deep dive.")


def _ws_reviews(ws: Any) -> None:
    if ws.reviews.available:
        st.success(ws.reviews.note)
        st.caption(
            "Pains, feature gaps, and improvement ideas come from the review-mining agents "
            "in a full Validate run."
        )
    else:
        st.info(ws.reviews.note)


def _ws_profit(ws: Any) -> None:
    p = ws.profit
    if not p.available or p.scenarios is None:
        st.info(p.note)
        return
    st.caption(p.note + f"  Profit uses **Profile: {ws.profile_name}** COGS/freight.")
    e = p.scenarios.expected
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Net margin", f"{(p.net_margin or 0) * 100:.0f}%")
    c2.metric("ROI", f"{(p.roi or 0) * 100:.0f}%")
    c3.metric("Units affordable", "—" if p.units_affordable is None else f"{p.units_affordable:,}")
    c4.metric("Break-even units", "—" if p.break_even_units is None else f"{p.break_even_units:,}")
    st.markdown(
        f"Fees: **{p.fee_source}** · FBA fulfilment {_usd(e.fees.fulfillment_cents / 100)} · "
        f"referral {_usd(e.fees.referral_cents / 100)} · landed {_usd(e.landed_cost_cents / 100)}"
    )
    st.caption(
        "Meets your profile targets."
        if p.meets_targets
        else "Below your profile margin/ROI targets (highlight only — not a hard gate)."
    )


def _ws_risk(ws: Any) -> None:
    d = ws.diagnosis
    if d is None:
        st.info("No verdict yet — run a Deep dive.")
        return
    _verdict_banner(d.verdict, f"opportunity {d.score:.0f}/100 · {d.confidence} confidence")
    if d.kill_reasons:
        st.subheader("Hard kills")
        for k in d.kill_reasons:
            st.markdown("- " + _escape_money(k))
    if d.gate_reasons:
        st.subheader("Gate failures")
        for g in d.gate_reasons:
            st.markdown("- " + _escape_money(g))
    st.subheader("Pillars")
    st.dataframe(
        [
            {
                "pillar": p.pillar,
                "score": p.capped,
                "confidence": p.confidence,
                "status": "absent" if not p.available else ("partial" if p.partial else "ok"),
                "driver": p.driver,
            }
            for p in d.pillars
        ],
        width="stretch",
        hide_index=True,
    )


# ---------------------------------------------------------------------------
# Settings (Research Profile + credentials + usage)
# ---------------------------------------------------------------------------
def page_settings() -> None:
    st.header("Settings")
    tab_profile, tab_creds = st.tabs(["Research Profile", "Credentials & Usage"])
    with tab_profile:
        _settings_profile()
    with tab_creds:
        st.subheader("API credentials")
        for p in credentials.provider_status():
            icon = "✅" if p.configured else "⬜"
            st.markdown(f"{icon} **{p.label}** — {p.enables}")
        st.caption("Values are read from the environment / .env and never shown.")
        st.divider()
        page_usage()


def _settings_profile() -> None:
    from delium.profile.models import CogsMode, ResearchProfile, RiskTolerance

    profiles = services.list_profiles()
    names = [p.name for p in profiles]
    active = _active_profile()
    idx = names.index(active.name) if active.name in names else 0
    chosen_name = st.selectbox("Preset", names, index=idx)
    chosen = next(p for p in profiles if p.name == chosen_name)

    if not chosen.is_active and st.button(f"Make “{chosen.name}” the active profile"):
        services.activate_profile(chosen.id or "")
        _refresh_profile()
        _goto("Settings")

    st.divider()
    st.markdown("**Edit profile** (drives finder defaults + profit; never the hard rules)")
    c1, c2, c3 = st.columns(3)
    budget = c1.number_input(
        "Budget (USD)", min_value=0.0, value=float(chosen.budget_usd or 0), step=500.0
    )
    margin = c2.number_input(
        "Target net margin",
        min_value=0.0,
        max_value=1.0,
        value=float(chosen.target_net_margin or 0.0),
        step=0.05,
    )
    roi = c3.number_input(
        "Target ROI (x)", min_value=0.0, value=float(chosen.target_roi or 0.0), step=0.25
    )
    c4, c5, c6 = st.columns(3)
    price_min = c4.number_input(
        "Price min ($)", min_value=0.0, value=float((chosen.price_min_cents or 0) / 100), step=1.0
    )
    price_max = c5.number_input(
        "Price max ($)", min_value=0.0, value=float((chosen.price_max_cents or 0) / 100), step=1.0
    )
    max_reviews = c6.number_input(
        "Max reviews", min_value=0, value=int(chosen.max_reviews or 0), step=50
    )
    c7, c8, c9 = st.columns(3)
    min_sales = c7.number_input(
        "Min monthly sales", min_value=0, value=int(chosen.min_monthly_sales or 0), step=50
    )
    max_weight = c8.number_input(
        "Max weight (g)", min_value=0, value=int(chosen.max_weight_g or 0), step=100
    )
    risk = c9.selectbox(
        "Risk tolerance",
        [r.value for r in RiskTolerance],
        index=[r.value for r in RiskTolerance].index(chosen.risk_tolerance.value),
    )
    c10, c11, c12 = st.columns(3)
    cogs_mode = c10.selectbox(
        "COGS mode",
        [m.value for m in CogsMode],
        index=[m.value for m in CogsMode].index(chosen.cogs_mode.value),
    )
    cogs_value = c11.number_input(
        "COGS value (pct 0-1 or $/unit)", min_value=0.0, value=float(chosen.cogs_value), step=0.05
    )
    freight = c12.number_input(
        "Freight ($/kg)", min_value=0.0, value=float(chosen.freight_per_kg_usd), step=0.5
    )
    marketplaces = st.multiselect(
        "Marketplaces", _MARKETPLACES, default=list(chosen.marketplaces) or ["US"]
    )
    excluded = st.text_input(
        "Excluded categories (comma-separated, soft filter)",
        value=", ".join(chosen.excluded_categories),
    )

    if st.button("Save profile", type="primary"):
        updated = ResearchProfile(
            id=chosen.id,
            name=chosen.name,
            is_active=chosen.is_active,
            budget_usd=budget or None,
            target_net_margin=margin or None,
            target_roi=roi or None,
            price_min_cents=int(price_min * 100) or None,
            price_max_cents=int(price_max * 100) or None,
            min_monthly_sales=int(min_sales) or None,
            max_reviews=int(max_reviews) or None,
            max_weight_g=int(max_weight) or None,
            max_size_tier=chosen.max_size_tier,
            preferred_categories=chosen.preferred_categories,
            excluded_categories=tuple(t.strip() for t in excluded.split(",") if t.strip()),
            marketplaces=tuple(marketplaces) or ("US",),
            cogs_mode=CogsMode(cogs_mode),
            cogs_value=cogs_value,
            freight_per_kg_usd=freight,
            risk_tolerance=RiskTolerance(risk),
        )
        services.save_profile(updated)
        _refresh_profile()
        st.success("Saved.")
        _goto("Settings")

    st.divider()
    new_name = st.text_input("New preset name")
    if st.button("Create preset") and new_name.strip():
        services.save_profile(ResearchProfile(name=new_name.strip()))
        _goto("Settings")
    if len(profiles) > 1 and st.button(f"Delete “{chosen.name}”"):
        services.delete_profile(chosen.id or "")
        _refresh_profile()
        _goto("Settings")


_SECTIONS = ["Home", "Find", "Product", "Settings"]
_SECTION_PAGES = {
    "Home": page_home,
    "Find": page_find,
    "Product": page_product,
    "Settings": page_settings,
}


def main() -> None:
    st.set_page_config(page_title="Delium", page_icon="🔎", layout="wide")
    st.session_state.setdefault("_section", "Home")
    st.sidebar.title("🔎 Delium")
    section = st.sidebar.radio(
        "Navigate", _SECTIONS, index=_SECTIONS.index(st.session_state["_section"])
    )
    st.session_state["_section"] = section
    st.sidebar.divider()
    _sidebar_credentials()
    _profile_banner()
    try:
        _SECTION_PAGES[section]()
    except ConfigError as exc:
        st.error(f"Configuration error: {exc}")


# Streamlit executes this script with __name__ == "__main__"; guarding keeps a
# plain `import delium.ui.app` (tooling/tests) side-effect free.
if __name__ == "__main__":
    main()
