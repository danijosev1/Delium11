"""Deterministic plain-English product card (no LLM).

Turns a scoring diagnosis + observable facts into a short, human-readable card:
the verdict in one sentence, why it looks promising (with numbers), why
confidence is low, what evidence would raise it, and the concrete next action.
Pure and rule-based — the same inputs always render the same words. Used by both
the CLI (`emerging` / `diagnose`) and the Streamlit UI so they never drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from delium.discovery.diagnostics import CandidateDiagnosis

# A pillar at/above this (capped) score is quoted as a reason it looks promising.
_PROMISING = 60.0


@dataclass(frozen=True)
class CardFacts:
    """Observable facts shown on the card. All optional — an absent fact is just
    omitted, never guessed."""

    title: str | None = None
    brand: str | None = None
    category: str | None = None
    price_cents: int | None = None
    bsr: int | None = None
    reviews: int | None = None
    age_days: int | None = None
    monthly_units: int | None = None
    emergence: float | None = None
    variation_count: int = 1
    established_brand: bool = False


@dataclass(frozen=True)
class ProductCard:
    asin: str
    headline: str
    promising: tuple[str, ...] = ()
    low_confidence: tuple[str, ...] = ()
    to_raise: tuple[str, ...] = ()
    next_action: str = ""
    flags: tuple[str, ...] = field(default_factory=tuple)

    def lines(self) -> list[str]:
        """Flat plain-text rendering (CLI). The UI can consume the fields directly."""
        out = [self.headline]
        for label, items in (
            ("Why it looks promising", self.promising),
            ("Why confidence is low", self.low_confidence),
            ("What would raise it", self.to_raise),
        ):
            if items:
                out.append(f"{label}:")
                out.extend(f"  - {it}" for it in items)
        for flag in self.flags:
            out.append(f"Note: {flag}")
        out.append(f"Next: {self.next_action}")
        return out


def _dollars(cents: int | None) -> str | None:
    return None if cents is None else f"${cents / 100:.2f}"


def _facts_line(facts: CardFacts) -> str | None:
    bits: list[str] = []
    price = _dollars(facts.price_cents)
    if price:
        bits.append(f"price {price}")
    if facts.bsr is not None:
        bits.append(f"BSR {facts.bsr:,}")
    if facts.monthly_units is not None:
        bits.append(f"~{facts.monthly_units:,} units/mo (Keepa)")
    if facts.reviews is not None:
        bits.append(f"{facts.reviews} reviews")
    if facts.age_days is not None:
        bits.append(f"{facts.age_days}d since first tracked")
    if facts.emergence is not None:
        bits.append(f"emergence {facts.emergence:.0f}/100")
    return " · ".join(bits) if bits else None


def _next_action(diag: CandidateDiagnosis) -> str:
    verdict = diag.verdict
    conf = diag.confidence
    if verdict == "avoid":
        if diag.kill_reasons:
            return f"Skip — hard-killed ({diag.kill_reasons[0]})."
        return "Skip for now — logged; a `watch` can revisit if the blocker is temporal."
    if verdict == "buy":
        return "Validate fully now — order samples and get real supplier quotes this week."
    # TEST
    if conf == "low":
        return (
            "Watch and re-check in ~3 weeks; validate fully once the evidence below is in "
            "(the score reflects only what is currently known)."
        )
    return "Validate fully — order competitor samples and get real supplier quotes to confirm."


def build_card(diag: CandidateDiagnosis, facts: CardFacts | None = None) -> ProductCard:
    """Assemble a deterministic card from a diagnosis + observable facts."""
    facts = facts or CardFacts()
    title = facts.title or diag.asin
    extra = facts.variation_count - 1
    head_name = title if facts.variation_count <= 1 else f"{title} (+{extra} variations)"
    headline = (
        f"{head_name} — {diag.verdict.upper()} at {diag.confidence.upper()} confidence "
        f"(opportunity {diag.score:.0f}/100)."
    )

    promising: list[str] = []
    facts_line = _facts_line(facts)
    if facts_line:
        promising.append(facts_line)
    for p in diag.pillars:
        if p.available and p.capped is not None and p.capped >= _PROMISING:
            promising.append(f"{p.pillar} {p.capped:.0f}/100 — {p.driver}")

    low_conf = [f"{c.pillar}: {c.cause}" for c in diag.causes]
    # Distinct fixes, ordered by first appearance, de-duplicated.
    seen: set[str] = set()
    to_raise: list[str] = []
    for c in diag.causes:
        if c.fix_key not in seen:
            seen.add(c.fix_key)
            to_raise.append(c.fix)

    flags: list[str] = []
    if facts.established_brand and facts.brand:
        flags.append(
            f"{facts.brand} is a large established brand — this 'new' listing is likely an "
            "ad-driven line extension, not an emerging underdog. Judge accordingly."
        )
    if diag.gate_reasons and diag.verdict != "avoid":
        flags.append("gate(s) not cleared: " + "; ".join(diag.gate_reasons))

    return ProductCard(
        asin=diag.asin,
        headline=headline,
        promising=tuple(promising),
        low_confidence=tuple(low_conf),
        to_raise=tuple(to_raise),
        next_action=_next_action(diag),
        flags=tuple(flags),
    )
