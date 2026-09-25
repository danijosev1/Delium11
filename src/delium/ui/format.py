"""Pure presentation helpers for the UI — no Streamlit, no I/O, no network.

They turn Delium's typed result objects and DB rows into plain dict/list
structures the Streamlit script renders (tables, the verdict banner, the
history chart). Keeping this logic here — not in `app.py` — is what makes the
UI unit-testable. Nothing here computes a score; it only reshapes existing
results for display.
"""

from __future__ import annotations

import sqlite3
from typing import Any

# Verdict → (display label, hex colour). Covers deterministic Buy/Test/Avoid and
# the cross-market verdict vocabulary.
_VERDICT_COLORS: dict[str, tuple[str, str]] = {
    "buy": ("BUY", "#1a7f37"),
    "test": ("TEST", "#9a6700"),
    "avoid": ("AVOID", "#cf222e"),
    "strong_opportunity": ("STRONG OPPORTUNITY", "#1a7f37"),
    "opportunity_to_validate": ("OPPORTUNITY TO VALIDATE", "#9a6700"),
    "mature_market": ("MATURE MARKET", "#57606a"),
    "weak_transfer": ("WEAK TRANSFER", "#cf222e"),
    "insufficient_data": ("INSUFFICIENT DATA", "#57606a"),
}


def verdict_style(verdict_value: str) -> tuple[str, str]:
    """(label, hex_colour) for a verdict string; grey fallback for unknowns."""
    return _VERDICT_COLORS.get(verdict_value, (verdict_value.upper(), "#57606a"))


# ---------------------------------------------------------------------------
# Money formatting for Streamlit markdown
# ---------------------------------------------------------------------------
def usd_md(amount: float, *, places: int = 2) -> str:
    """A USD amount for Streamlit markdown with the '$' escaped.

    Streamlit renders `$...$` as LaTeX math, so an unescaped '$' (or a pair around
    an en-dash, e.g. `$0.00–$2.20`) garbles cost text into an equation. Escaping
    the sign keeps it literal."""
    return f"\\${amount:,.{places}f}"


def escape_money(text: str) -> str:
    """Escape every literal '$' in a free-text string so Streamlit markdown does
    not render it (or a pair of them) as LaTeX math."""
    return text.replace("$", "\\$")


def _usd(cents: int | None) -> float | None:
    return None if cents is None else round(cents / 100, 2)


# ---------------------------------------------------------------------------
# Keyword research
# ---------------------------------------------------------------------------
def related_rows(result: Any) -> list[dict[str, Any]]:
    ranked = sorted(result.related, key=lambda k: (k.volume is None, -(k.volume or 0)))
    return [{"keyword": k.phrase, "volume": k.volume} for k in ranked]


def serp_rows(result: Any) -> list[dict[str, Any]]:
    return [
        {
            "position": item.position,
            "asin": item.asin,
            "sponsored": item.sponsored,
            "price_usd": _usd(item.price_cents),
            "title": item.title,
        }
        for item in sorted(result.serp, key=lambda s: s.position)
    ]


# ---------------------------------------------------------------------------
# Product lookup
# ---------------------------------------------------------------------------
def history_series(rows: list[sqlite3.Row]) -> dict[str, list[Any]]:
    """Aligned price/BSR series for a line chart, oldest→newest."""
    dates: list[str] = []
    price: list[float | None] = []
    bsr: list[int | None] = []
    for r in rows:
        dates.append(r["captured_on"])
        price.append(_usd(r["price_cents"]))
        bsr.append(None if r["bsr"] is None else int(r["bsr"]))
    return {"date": dates, "price_usd": price, "bsr": bsr}


# ---------------------------------------------------------------------------
# Validate — deterministic score breakdown
# ---------------------------------------------------------------------------
def pillar_rows(scored: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in scored.pillars:
        rows.append(
            {
                "pillar": p.pillar,
                "raw": None if p.raw_score is None else round(p.raw_score, 1),
                "capped": None if p.capped_score is None else round(p.capped_score, 1),
                "weight": p.weight,
                "contribution": round(p.weighted_contribution, 1),
                "confidence": p.confidence.value,
                "status": "absent" if not p.available else ("partial" if p.partial else "ok"),
            }
        )
    return rows


def kill_rows(scored: Any) -> list[dict[str, Any]]:
    """Triggered + borderline hard kills (the ones that affect the verdict)."""
    out: list[dict[str, Any]] = []
    for k in scored.kills:
        if k.kills or (k.assessed and k.triggered and k.demoted):
            out.append(
                {
                    "rule": k.rule_id,
                    "name": k.name,
                    "effect": "KILL" if k.kills else "→Test (borderline)",
                    "actual": k.actual or "",
                    "threshold": k.threshold or "",
                }
            )
    return out


def gate_rows(scored: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for g in scored.gates:
        state = "pending" if g.passed is None else ("pass" if g.passed else "FAIL")
        out.append(
            {
                "gate": g.gate_id,
                "name": g.name,
                "kind": "hard" if g.hard else "soft",
                "state": state,
                "actual": g.actual or "",
            }
        )
    return out


# ---------------------------------------------------------------------------
# Discover / cross-market / history
# ---------------------------------------------------------------------------
def discovery_rows(report: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ec in report.ranked:
        s = ec.scored
        if s is None:
            continue
        rows.append(
            {
                "asin": ec.asin,
                "marketplace": ec.marketplace.value,
                "score": round(s.score, 0),
                "verdict": s.verdict.value,
                "confidence": s.confidence.level.value,
                "needs_data": bool(s.insufficient_data),
                "via": ",".join(src.value for src in ec.candidate.sources),
            }
        )
    return rows


def discovery_killed_rows(report: Any, facts: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Every cheap-killed candidate with the exact rule(s) + observed/threshold
    values. Title/price/BSR come from `facts` (the hydrated product), since the
    kill happens on that product row, not the SERP row."""
    rows: list[dict[str, Any]] = []
    for ec in report.killed:
        f = facts.get(ec.asin, {})
        scored = ec.scored
        triggered = [k for k in (scored.kills if scored is not None else ()) if k.kills]
        rule = "; ".join(f"{k.rule_id} {k.name}" for k in triggered) or (ec.kill_rule or "")
        actual = "; ".join(k.actual for k in triggered if k.actual)
        threshold = "; ".join(k.threshold for k in triggered if k.threshold)
        rows.append(
            {
                "asin": ec.asin,
                "title": f.get("title") or "—",
                "price_usd": _usd(f.get("price_cents")),
                "bsr": f.get("bsr"),
                "kill_rule": rule,
                "actual": actual,
                "threshold": threshold,
                "reason": ec.notes[0] if ec.notes else "",
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Usage page (token / spend reporting)
# ---------------------------------------------------------------------------
def spend_rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Per-day, per-provider spend + tokens (from repository.spend_by_provider_day)."""
    return [
        {
            "day": r["day"],
            "provider": r["provider"],
            "calls": int(r["calls"]),
            "cost_usd": round(float(r["cost_usd"]), 4),
            "tokens": int(r["tokens"]),
        }
        for r in rows
    ]


def run_type_rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Per-command averages (from repository.run_type_summary)."""
    return [
        {
            "command": r["command"],
            "runs": int(r["runs"]),
            "avg_data_usd": round(float(r["avg_data_usd"] or 0.0), 4),
            "avg_llm_usd": round(float(r["avg_llm_usd"] or 0.0), 4),
            "avg_tokens": round(float(r["avg_tokens"] or 0.0), 1),
        }
        for r in rows
    ]


def cross_market_rows(candidates: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for c in candidates:
        r = c.report
        te = r.target_evidence
        rows.append(
            {
                "source_asin": c.source_asin,
                "title": (r.match.source.title or c.source_asin),
                "route": f"{c.source_marketplace.value}→{c.target_marketplace.value}",
                "verdict": r.verdict.value,
                "score": round(r.score, 0),
                "confidence": r.confidence.level.value,
                "match": r.match.confidence.value,
                "target_presence": te.presence.value,
                "target_demand": round(te.target_demand_score, 0),
                "demand_credible": bool(te.demand_credible),
                "competition_gap": round(r.market_gap.competition_gap, 0),
            }
        )
    return rows


def emerging_rows(candidates: list[Any]) -> list[dict[str, Any]]:
    """Scored emerging candidates → table rows (emergence + opportunity)."""
    rows: list[dict[str, Any]] = []
    for c in candidates:
        s = c.evaluated.scored
        rows.append(
            {
                "asin": c.asin,
                "emergence": None
                if c.emergence.emergence_score is None
                else round(c.emergence.emergence_score, 0),
                "age_days": c.emergence.age_days,
                "opportunity": None if s is None else round(s.score, 0),
                "verdict": None if s is None else s.verdict.value,
                "confidence": None if s is None else s.confidence.level.value,
                "why": "; ".join(c.emergence.reasons),
            }
        )
    return rows


def emerging_killed_rows(candidates: list[Any]) -> list[dict[str, Any]]:
    """Emerging-but-killed candidates → table rows with the exact kill reason."""
    return [
        {
            "asin": c.asin,
            "emergence": None
            if c.emergence.emergence_score is None
            else round(c.emergence.emergence_score, 0),
            "kill_rule": c.evaluated.kill_rule,
            "reason": c.evaluated.notes[0] if c.evaluated.notes else "",
        }
        for c in candidates
    ]


def emerging_candidate_rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Persisted emerging_candidates rows → table (History page)."""
    return [
        {
            "asin": r["asin"],
            "emergence": r["emergence_score"],
            "age_days": r["age_days"],
            "outcome": r["outcome"],
            "opportunity": r["opportunity_score"],
            "verdict": r["verdict"],
            "kill_rule": r["kill_rule"],
        }
        for r in rows
    ]


def run_rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [
        {
            "started_at": r["started_at"],
            "command": r["command"],
            "input": r["input"],
            "status": r["status"],
            "data_usd": round(float(r["data_cost_usd"]), 4),
            "llm_usd": round(float(r["llm_cost_usd"]), 4),
        }
        for r in rows
    ]


def validation_rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [
        {
            "created_at": r["created_at"],
            "asin": r["asin"],
            "marketplace": r["marketplace"],
            "verdict": r["verdict"],
            "score": (
                None if r["opportunity_score"] is None else round(float(r["opportunity_score"]), 0)
            ),
            "confidence": r["confidence"],
            "needs_data": bool(r["insufficient_data"]),
        }
        for r in rows
    ]
