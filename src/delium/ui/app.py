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
    marketplace = col1.selectbox("Marketplace", _MARKETPLACES, key="kw_mp")
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
    st.dataframe(format.serp_rows(result), width="stretch", hide_index=True)


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
    marketplace = col1.selectbox("Marketplace", _MARKETPLACES, key="disc_mp")
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
    col1, col2 = st.columns(2)
    marketplace = col1.selectbox("Marketplace", _MARKETPLACES, key="em_mp")
    cats_raw = col2.text_input("Keepa category ids (comma-separated, optional)", key="em_cats")
    col3, col4 = st.columns(2)
    max_reviews = col3.number_input(
        "Max reviews override", min_value=0, value=0, step=10, key="em_rev"
    )
    max_age = col4.number_input(
        "Max age days override", min_value=0, value=0, step=30, key="em_age"
    )
    category_ids = [int(x) for x in cats_raw.replace(" ", "").split(",") if x.strip().isdigit()]
    overrides: dict[str, int] = {}
    if max_reviews:
        overrides["reviews_max"] = int(max_reviews)
    if max_age:
        overrides["age_max_days"] = int(max_age)

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


_PAGES = {
    "Keyword research": page_keyword_research,
    "Product lookup": page_product_lookup,
    "Validate": page_validate,
    "Discover": page_discover,
    "Emerging": page_emerging,
    "Cross-market": page_cross_market,
    "History": page_history,
    "Usage": page_usage,
}


def main() -> None:
    st.set_page_config(page_title="Delium", page_icon="🔎", layout="wide")
    st.sidebar.title("🔎 Delium")
    choice = st.sidebar.radio("Page", list(_PAGES), label_visibility="collapsed")
    st.sidebar.divider()
    _sidebar_credentials()
    try:
        _PAGES[choice]()
    except ConfigError as exc:
        st.error(f"Configuration error: {exc}")


# Streamlit executes this script with __name__ == "__main__"; guarding keeps a
# plain `import delium.ui.app` (tooling/tests) side-effect free.
if __name__ == "__main__":
    main()
