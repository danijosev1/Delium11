"""Validation report rendering (docs/agent-layer.md §6).

Composes a human-readable report from **typed objects only** — the
`ValidationReport` (deterministic `ScoredOpportunity` + validated agent outputs) —
never from freeform model text. Deterministic numbers are clearly separated from
the Strategist's advisory narrative, and every quote shown is fetched from the
`reviews` table by id (passed in as `quotes`), never re-emitted model text.

Output is plain text (Markdown), so LLM-authored strings cannot inject terminal
or markup control sequences — the untrusted-text firewall holds at the renderer.
A report is regenerable from the DB with no LLM call: this function takes only
stored data.
"""

from __future__ import annotations

from collections.abc import Mapping

from delium.analysis.models import ScoredOpportunity
from delium.validation.models import ValidationReport, ValidationStatus

# id -> (stars, text) fetched from the reviews table by the caller.
Quotes = Mapping[str, tuple[int, str]]


def quote_ids(report: ValidationReport) -> list[str]:
    """Review ids the report will want to quote — the representative ids of the
    counted complaint themes. The caller resolves these against the reviews table."""
    if report.differentiation is None or report.miner_report is None:
        return []
    ids: list[str] = []
    counted = {t.label for t in report.differentiation.themes if t.counted}
    for c in report.miner_report.complaints:
        if c.theme in counted:
            ids.extend(c.representative_quote_ids[:2] or c.quote_review_ids[:2])
    return ids


def render_validation(
    report: ValidationReport, *, product_title: str | None = None, quotes: Quotes | None = None
) -> str:
    """Render the full validation report as Markdown text."""
    quotes = quotes or {}
    lines: list[str] = []

    def _p(text: str = "") -> None:
        lines.append(text)

    asin = report.asin or "—"
    _p(f"# Validation — {asin} [{report.marketplace.value}]")
    if product_title:
        _p(f"_{_flat(product_title)}_")
    _p()
    _p(
        "> The Buy/Test/Avoid verdict below is the deterministic output of "
        "scoring.py. The Strategist section is advisory narrative — it explains "
        "and argues, it does not set the verdict."
    )
    _p()

    if report.status not in (ValidationStatus.SCORED, ValidationStatus.HARD_KILLED):
        _p(f"**No verdict produced** — status: `{report.status.value}`.")
        for note in report.notes:
            _p(f"- {_flat(note)}")
        return "\n".join(lines)

    scored = report.scored
    assert scored is not None
    _verdict_section(_p, report, scored)
    _pillars_section(_p, scored)
    _kills_gates_section(_p, scored)
    _customer_pain_section(_p, report, quotes)
    _features_bundles_section(_p, report)
    _market_analyst_section(_p, report)
    _risks_section(_p, report, scored)
    _strategist_section(_p, report)
    _methodology_section(_p, report)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
def _verdict_section(_p, report: ValidationReport, scored: ScoredOpportunity) -> None:  # type: ignore[no-untyped-def]
    _p("## Verdict")
    suff = "insufficient data" if scored.insufficient_data else "sufficient data"
    _p(
        f"**{scored.verdict.value.upper()}**  ·  opportunity score "
        f"{scored.score:.0f}/100  ·  {scored.confidence.level.value} confidence  ·  {suff}"
    )
    g5 = next((g for g in scored.gates if g.gate_id == "G5"), None)
    if g5 is not None:
        state = "pending" if g5.passed is None else ("met" if g5.passed else "not met")
        _p(f"- G5 Strategist concurrence: **{state}** ({_flat(g5.actual or '')})")
    if scored.strategist_pending:
        _p("- A Buy here would be provisional pending Strategist concurrence.")
    # Disagreement banner (pessimist-wins is surfaced, never averaged).
    sv = report.strategist_verdict
    if sv is not None and (not sv.agrees_with_score or sv.verdict != scored.verdict.value):
        _p(
            f"- ⚠️ Disagreement: deterministic verdict **{scored.verdict.value.upper()}**, "
            f"Strategist argues **{sv.verdict.upper()}** — the more cautious call wins the "
            "default; see the Strategist section."
        )
    if report.status is ValidationStatus.HARD_KILLED:
        kill = next((k for k in scored.kills if k.kills), None)
        if kill is not None:
            _p(
                f"- Hard kill **{kill.rule_id}** ({_flat(kill.name)}): {_flat(kill.reason)} "
                "— review/LLM spend skipped."
            )
    _p()


def _pillars_section(_p, scored: ScoredOpportunity) -> None:  # type: ignore[no-untyped-def]
    _p("## Pillars (deterministic)")
    _p("| Pillar | Raw | Capped | Weight | Contribution | Conf | Note |")
    _p("|---|---|---|---|---|---|---|")
    for p in scored.pillars:
        raw = "—" if p.raw_score is None else f"{p.raw_score:.0f}"
        capped = "—" if p.capped_score is None else f"{p.capped_score:.0f}"
        note = "absent" if not p.available else ("partial" if p.partial else "")
        _p(
            f"| {p.pillar} | {raw} | {capped} | {p.weight:.0f} | "
            f"{p.weighted_contribution:.1f} | {p.confidence.value} | {note} |"
        )
    _p()


def _kills_gates_section(_p, scored: ScoredOpportunity) -> None:  # type: ignore[no-untyped-def]
    triggered = [k for k in scored.kills if k.kills]
    demoted = [k for k in scored.kills if k.assessed and k.triggered and k.demoted]
    unassessed = sum(1 for k in scored.kills if not k.assessed)
    _p("## Hard kills & gates (deterministic)")
    _p(
        f"Hard kills: {len(triggered)} triggered, {len(demoted)} borderline (→Test), "
        f"{unassessed} unassessed."
    )
    for k in triggered:
        actual, threshold = _flat(k.actual or ""), _flat(k.threshold or "")
        _p(f"- **{k.rule_id}** {_flat(k.name)}: {actual} vs {threshold}")
    for g in scored.gates:
        if g.passed is None:
            state = "pending"
        elif g.passed:
            state = "pass"
        else:
            state = "FAIL"
        kind = "hard" if g.hard else "soft"
        _p(f"- {g.gate_id} {_flat(g.name)} ({kind}): {state}")
    _p()


def _customer_pain_section(_p, report: ValidationReport, quotes: Quotes) -> None:  # type: ignore[no-untyped-def]
    _p("## Customer pain (Review Miner)")
    re_ = report.review_evidence
    if re_ is None:
        _p("_No review sample — differentiation evidence is absent._")
        _p()
        return
    miner_state = "not run (deterministic evidence only)" if re_.miner_pending else "ran"
    _p(
        f"Review Miner: **{miner_state}**  ·  sample {re_.sample_size} reviews  ·  "
        f"{re_.themes_available} theme(s)."
    )
    diff = report.differentiation
    if diff is not None:
        if diff.sample_bias_flag:
            _p(
                "- ⚠️ Sample skews positive vs the listing rating — complaints are likely "
                "under-represented (F1 bias-adjusted)."
            )
        if diff.confidence.level.value == "low":
            _p("- ⚠️ Low differentiation confidence (thin sample or weak evidence).")
        counted = [t for t in diff.themes if t.counted]
        if counted:
            _p("\n**Complaint themes** (frequencies RECOMPUTED from cited review ids):")
            for t in sorted(counted, key=lambda x: x.intensity, reverse=True)[:6]:
                sev = "—" if t.severity is None else str(t.severity)
                _p(
                    f"- {_flat(t.label)} — {t.frequency * 100:.0f}% of sample, severity {sev}, "
                    f"addressability {t.addressability.value} "
                    f"({t.verified_count} verified reviews)"
                )
                quote = _first_quote(report, t.label, quotes)
                if quote is not None:
                    stars, text = quote
                    _p(f'    > "{_flat(text)[:200]}" — {stars}★')
        else:
            _p("- No complaint theme cleared the ≥3-verified-quote evidence bar.")
    if report.miner_report is not None:
        for caveat in report.miner_report.sample_caveats[:3]:
            _p(f"- Miner caveat: {_flat(caveat)}")
    _p()


def _features_bundles_section(_p, report: ValidationReport) -> None:  # type: ignore[no-untyped-def]
    mr = report.miner_report
    re_ = report.review_evidence
    gaps = re_.feature_gaps if re_ is not None else ()
    if mr is None or (not mr.missing_features and not mr.bundle_signals):
        return
    _p("## Product opportunities (Review Miner)")
    if gaps:
        # Present/Absent/Unknown against the Analyst competitor matrix — a gap is
        # only 'absent' when confirmed, never inferred from silence.
        confirmed = re_ is not None and re_.competitor_matrix_confirmed
        note = "" if confirmed else " (competitor matrix unconfirmed → all Unknown)"
        _p(f"**Requested features vs. competitors{note}:**")
        _p("| Feature | Requests | Competitor status |")
        _p("|---|---|---|")
        label = {"absent": "Absent (confirmed gap)", "present": "Present", "unknown": "Unknown"}
        for g in sorted(gaps, key=lambda x: (x.status != "absent", -x.request_count))[:8]:
            _p(f"| {_flat(g.feature)} | {g.request_count} | {label.get(g.status, 'Unknown')} |")
    elif mr.missing_features:
        _p("**Requested features (absence not competitor-confirmed):**")
        for f in mr.missing_features[:6]:
            _p(f"- {_flat(f.feature)} ({len(f.requested_in_review_ids)} requests)")
    if mr.bundle_signals:
        _p("**Bundle / packaging opportunities:**")
        for b in mr.bundle_signals[:4]:
            _p(f"- {_flat(b.complement)} ({len(b.mentioned_in_review_ids)} mentions)")
    _p()


def _market_analyst_section(_p, report: ValidationReport) -> None:  # type: ignore[no-untyped-def]
    """Analyst competitive read — interpretive narrative + the observable
    competitor feature matrix. Carries no score; the confirmed feature gaps that
    move the differentiation pillar are shown above and recomputed by the engine."""
    ar = report.analyst_report
    if ar is None:
        return
    _p("## Market & competition (Analyst — advisory)")
    _p(
        f"Market structure: **{ar.market_structure.type}**  ·  attractiveness: "
        f"**{ar.attractiveness.rating}**."
    )
    if ar.attractiveness.one_line:
        _p(f"- {_flat(ar.attractiveness.one_line)}")
    for o in ar.openings[:4]:
        _p(f"- Opening: {_flat(o.description)}")
    for c in ar.concerns[:4]:
        _p(f"- Concern: {_flat(c.description)}")
    matrix = [e for e in ar.feature_matrix if e.claimed_features]
    if matrix:
        _p(
            "\n**Competitor feature matrix** (features CLAIMED in each listing; absence of a "
            "claim is not proof of product absence):"
        )
        _p("| ASIN | Claimed features |")
        _p("|---|---|")
        for e in matrix[:10]:
            feats = ", ".join(_flat(f) for f in e.claimed_features[:8]) or "—"
            _p(f"| {_flat(e.asin)} | {feats} |")
    for gap in ar.data_gaps_acknowledged[:3]:
        _p(f"- Data gap: {_flat(gap)}")
    _p()


def _risks_section(_p, report: ValidationReport, scored: ScoredOpportunity) -> None:  # type: ignore[no-untyped-def]
    risk_pillar = next((p for p in scored.pillars if p.pillar == "risk"), None)
    deterministic = [c for c in risk_pillar.components if (c.value or 0) > 0] if risk_pillar else []
    sv = report.strategist_verdict
    if not deterministic and (sv is None or not sv.risk_register):
        return
    _p("## Risks")
    for c in deterministic:
        _p(f"- (deterministic) {_flat(c.name)}: {_flat(c.detail)}")
    if sv is not None:
        for r in sv.risk_register:
            _p(
                f"- (strategist) {_flat(r.risk)} — likelihood {r.likelihood}, impact "
                f"{r.impact}; mitigation: {_flat(r.mitigation)}"
            )
    _p()


def _strategist_section(_p, report: ValidationReport) -> None:  # type: ignore[no-untyped-def]
    sv = report.strategist_verdict
    _p("## Strategist recommendation (advisory — does not set the verdict)")
    if sv is None:
        pending = any(r.agent == "strategist" for r in report.agent_runs)
        _p(
            "_Strategist did not produce a review; the deterministic verdict stands and a "
            "Buy cannot be confirmed._"
            if pending
            else "_Strategist not run (agents disabled)._"
        )
        _p()
        return
    _p(
        f"Strategist read: **{sv.verdict.upper()}** (conviction {sv.conviction}/5, "
        f"agrees_with_score={sv.agrees_with_score})."
    )
    _p(f"\n{_flat(sv.one_paragraph)}\n")
    if sv.differentiation_plan:
        _p("**Differentiation plan:**")
        for d in sv.differentiation_plan:
            _p(f"- {_flat(d.change)} (COGS impact: {d.cogs_impact})")
    _p("**What would change the verdict:**")
    for vc in sv.verdict_changers:
        _p(f"- {_flat(vc.fact_that_would_flip)} → {_flat(vc.how_to_obtain_it)}")
    if sv.assumption_challenges:
        _p("**Assumption challenges:**")
        for ac in sv.assumption_challenges:
            _p(f"- {_flat(ac.assumption_flag_ref)}: {_flat(ac.why_questionable)}")
    _p()


def _methodology_section(_p, report: ValidationReport) -> None:  # type: ignore[no-untyped-def]
    _p("## Methodology & data quality")
    h = report.hydration
    _p(
        f"- Data cost: ${h.data_cost_usd:.4f}  ·  LLM cost: ${h.llm_cost_usd:.4f}  ·  "
        f"reviews fetched: {h.reviews_fetched}  ·  competitors: {h.competitors_hydrated}"
    )
    if h.degraded:
        _p("- ⚠️ Run degraded (budget or provider/agent failure) — see notes.")
    for run in report.agent_runs:
        detail = f"{run.dropped}/{run.total} dropped" if run.total else ""
        err = f" — {_flat(run.error)}" if run.error else ""
        _p(
            f"- Agent `{run.agent}`: {run.status} "
            f"({run.model or '—'}, ${run.cost_usd:.4f}, {detail}){err}"
        )
    for note in report.notes:
        _p(f"- {_flat(note)}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _flat(text: str) -> str:
    """Collapse whitespace and strip markup/control characters from any string
    that may originate from an LLM or a review — the untrusted-text firewall."""
    return " ".join(str(text).split()).replace("`", "'").replace("|", "/")


def _first_quote(report: ValidationReport, label: str, quotes: Quotes) -> tuple[int, str] | None:
    if report.miner_report is None:
        return None
    for c in report.miner_report.complaints:
        if c.theme != label:
            continue
        for rid in (*c.representative_quote_ids, *c.quote_review_ids):
            if rid in quotes:
                return quotes[rid]
    return None
