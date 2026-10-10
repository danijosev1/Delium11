"""FIX 3 — the scan report renders Rich markup properly (never prints raw
`[cyan]…` tags), escapes dynamic data so a category/reason can't inject markup,
and shows Amazon links for both finalists and the unranked candidates."""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from delium.cli import main as cli_main
from delium.discovery.daily_scan import ScanReport


def _render(report: ScanReport, monkeypatch: pytest.MonkeyPatch) -> str:
    buf = io.StringIO()
    cap = Console(file=buf, force_terminal=False, no_color=True, width=100)
    monkeypatch.setattr(cli_main, "console", cap)
    cli_main._render_scan(report)
    return buf.getvalue()


def _report() -> ScanReport:
    return ScanReport(
        scan_id="scan123",
        status="complete",
        marketplaces=("UK",),
        funnel={
            "swept": 3,
            "hydrated": 3,
            "killed": 0,
            "scored": 3,
            "competitor_set": 0,
            "finalists": 1,
        },
        stages=[],
        finalists=[
            {
                "rank": 1,
                "asin": "B0FIN00001",
                "marketplace": "UK",
                "sellability": 55.0,
                "opportunity": 60.0,
                "confidence": "low",
                "differentiation": "pending",
                "reason": "Sellability 55/100: opportunity 60/100.",
            }
        ],
        breakdown={"ranked — below finalist cutoff": 2},
        unranked=[
            {
                "asin": "B0UNR00001",
                "marketplace": "UK",
                "sellability": 40.0,
                "confidence": "low",
                "reason": "Ranked #2 by sellability — below the top 1 finalist cutoff.",
            }
        ],
        # Category text with literal brackets must be escaped, not interpreted.
        categories=[{"category": "Home & Garden [sale]", "score": 72.0, "reason": "strong [pull]"}],
    )


def test_render_escapes_markup_and_shows_links(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _render(_report(), monkeypatch)

    # Markup tags are rendered (consumed), never printed literally — the raw-markup
    # bug showed "[cyan]Home & Garden[/cyan]" on the terminal.
    assert "[cyan]" not in out and "[/cyan]" not in out and "[bold]" not in out
    # Dynamic category/reason text keeps its own brackets verbatim (escaped, so Rich
    # does not treat "[sale]" / "[pull]" as markup).
    assert "Home & Garden [sale]" in out
    assert "strong [pull]" in out
    # Amazon links for the finalist AND the unranked candidate.
    assert "https://www.amazon.co.uk/dp/B0FIN00001" in out
    assert "https://www.amazon.co.uk/dp/B0UNR00001" in out
    # The funnel tail is always explained.
    assert "Not finalists" in out
    assert "ranked — below finalist cutoff" in out
    assert "Unranked" in out


def test_render_handles_empty_finalists(monkeypatch: pytest.MonkeyPatch) -> None:
    report = _report()
    report.finalists = []
    report.funnel["finalists"] = 0
    out = _render(report, monkeypatch)
    assert "none" in out  # finalists section still renders
    assert "Not finalists" in out  # and the breakdown still explains the tail
