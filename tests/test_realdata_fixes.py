"""Tests for the real-data fixes surfaced on live Mac runs.

Cover, with no network and no paid calls (fake transports / fixtures only):
  * configurable per-provider read timeouts (item 1),
  * batched, cache-first Keepa hydration collapsing N single-ASIN calls into one
    (item 2), plus the free `/token` balance read used by the Usage page,
  * bare-ASIN detection tolerating case and whitespace (item 5),
  * Usage-page aggregations from the fetch ledger (item 6),
  * the discover "killed candidate" table + money escaping (items 4, 7).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from delium.config.models import DeliumConfig
from delium.database import get_connection, repository
from delium.providers.dataforseo import DataForSeoClient
from delium.providers.keepa import KeepaClient
from delium.providers.reviews import ApifyClient, UnwrangleClient
from delium.ui import format
from delium.validation import looks_like_asin
from keepa_support import DEFAULT_ASIN, FakeTransport, keepa_product_body, ok

CFG = DeliumConfig()


# ---------------------------------------------------------------------------
# Item 5 — bare-ASIN detection (case + surrounding whitespace)
# ---------------------------------------------------------------------------
def test_looks_like_asin_accepts_case_and_whitespace() -> None:
    assert looks_like_asin("B0F543R23M") == "B0F543R23M"
    assert looks_like_asin(" b0f543r23m ") == "B0F543R23M"  # lower + padding
    assert looks_like_asin("\tB0f543R23m\n") == "B0F543R23M"


def test_looks_like_asin_rejects_non_asins() -> None:
    assert looks_like_asin("silicone baby food tray") is None  # keyword
    assert looks_like_asin("ABCDEFGHIJ") is None  # 10 letters, no digit → keyword
    assert looks_like_asin("B0F543R23") is None  # 9 chars
    assert looks_like_asin("B0F543R23MX") is None  # 11 chars
    assert looks_like_asin("") is None


# ---------------------------------------------------------------------------
# Item 1 — configurable read timeouts per provider
# ---------------------------------------------------------------------------
def test_provider_timeouts_are_configurable() -> None:
    keepa = KeepaClient("k", timeout=123.0)
    dfs = DataForSeoClient("login", "pw", timeout=99.0)
    unwrangle = UnwrangleClient("u", timeout=45.0)
    apify = ApifyClient("t", timeout=77.0)
    # The timeout is threaded into the default UrllibTransport.
    assert keepa._transport._timeout == 123.0  # type: ignore[attr-defined]
    assert dfs._transport._timeout == 99.0  # type: ignore[attr-defined]
    assert unwrangle._transport._timeout == 45.0  # type: ignore[attr-defined]
    assert apify._transport._timeout == 77.0  # type: ignore[attr-defined]


def test_dataforseo_live_serp_timeout_default_is_generous() -> None:
    # Live Amazon SERP can take 20–60s; the default read timeout must clear that.
    dfs = DataForSeoClient("login", "pw")
    assert dfs._transport._timeout >= 120.0  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Item 2 — batched Keepa hydration + free token status
# ---------------------------------------------------------------------------
def test_hydrate_products_batches_into_one_keepa_call(initialized_db: Path) -> None:
    from delium.ingestion import hydrate_products

    asins = [DEFAULT_ASIN, "B0SECOND002", "B0THIRD0003"]
    body = keepa_product_body(tokens_consumed=6)
    body["products"] = [{**body["products"][0], "asin": a} for a in asins]
    transport = FakeTransport([ok(body)])
    client = KeepaClient("k", transport=transport, sleep=lambda _s: None, marketplace="US")

    with get_connection() as conn:
        run_id = repository.insert_run(conn, command="discover", input_="x")
    views = hydrate_products(asins, run_id=run_id, client=client, config=CFG)

    assert set(views) == set(asins)
    assert transport.call_count == 1  # one batched call, not one per ASIN


def test_hydrate_products_is_cache_first(initialized_db: Path) -> None:
    from delium.ingestion import hydrate_products

    body = keepa_product_body(DEFAULT_ASIN)
    transport = FakeTransport([ok(body)])
    client = KeepaClient("k", transport=transport, sleep=lambda _s: None, marketplace="US")
    with get_connection() as conn:
        run_id = repository.insert_run(conn, command="discover", input_="x")

    hydrate_products([DEFAULT_ASIN], run_id=run_id, client=client, config=CFG)
    hydrate_products([DEFAULT_ASIN], run_id=run_id, client=client, config=CFG)  # cached now
    assert transport.call_count == 1  # second call served from cache, no Keepa hit


def test_token_status_reads_balance_without_leaking_key() -> None:
    transport = FakeTransport([ok({"tokensLeft": 240, "refillRate": 20, "refillIn": 60000})])
    client = KeepaClient("secret-key", transport=transport, sleep=lambda _s: None)
    status = client.token_status()
    assert status.tokens_left == 240
    assert status.refill_rate == 20
    # A /token call must not spend tokens: no product params in the request.
    assert transport.calls and "asin" not in transport.calls[0]


# ---------------------------------------------------------------------------
# Item 6 — Usage-page aggregations from the fetch ledger
# ---------------------------------------------------------------------------
def test_run_token_total_and_provider_spend(initialized_db: Path) -> None:
    with get_connection() as conn:
        run_id = repository.insert_run(conn, command="discover", input_="x")
        repository.insert_raw_fetch(
            conn,
            run_id=run_id,
            provider="keepa",
            endpoint="product",
            request_key="keepa:product:US:B0AAA00001",
            payload={"asin": "B0AAA00001"},
            tokens_used=6,
        )
        repository.insert_raw_fetch(
            conn,
            run_id=run_id,
            provider="dataforseo",
            endpoint="serp",
            request_key="dfs:serp:x",
            payload={},
            cost_usd=0.03,
        )
        assert repository.run_token_total(conn, run_id) == 6
        spend = format.spend_rows(repository.spend_by_provider_day(conn, days=30))
        by_provider = {r["provider"]: r for r in spend}
        assert by_provider["keepa"]["tokens"] == 6
        assert by_provider["dataforseo"]["cost_usd"] == 0.03
        summary = {r["command"]: r for r in format.run_type_rows(repository.run_type_summary(conn))}
        assert summary["discover"]["avg_tokens"] == 6.0


# ---------------------------------------------------------------------------
# Items 4 & 7 — money escaping + killed-candidate table
# ---------------------------------------------------------------------------
def test_money_helpers_escape_dollar_signs() -> None:
    assert format.usd_md(0) == "\\$0.00"
    assert format.usd_md(2.2) == "\\$2.20"
    # A cost band that used to render as LaTeX is now literal.
    assert "$" not in format.escape_money("bounded by $2.20/discover").replace("\\$", "")


def test_discovery_killed_rows_show_rule_and_values() -> None:
    kill = SimpleNamespace(
        rule_id="K3",
        name="unbeatable brand moat",
        actual="brand share 0.82",
        threshold="0.60",
        kills=True,
    )
    ec = SimpleNamespace(
        asin="B0KILL0001",
        marketplace=SimpleNamespace(value="US"),
        scored=SimpleNamespace(kills=[kill]),
        kill_rule="K3",
        notes=("brand share too high",),
    )
    report = SimpleNamespace(killed=[ec])
    facts = {"B0KILL0001": {"title": "Widget", "price_cents": 1999, "bsr": 4200}}
    rows = format.discovery_killed_rows(report, facts)
    assert len(rows) == 1
    row = rows[0]
    assert row["asin"] == "B0KILL0001"
    assert row["title"] == "Widget"
    assert row["price_usd"] == 19.99
    assert row["bsr"] == 4200
    assert "K3" in row["kill_rule"] and "moat" in row["kill_rule"]
    assert row["actual"] == "brand share 0.82"
    assert row["threshold"] == "0.60"


def test_discovery_killed_rows_tolerate_missing_facts() -> None:
    ec = SimpleNamespace(
        asin="B0KILL0002",
        marketplace=SimpleNamespace(value="US"),
        scored=None,
        kill_rule="K1",
        notes=(),
    )
    report = SimpleNamespace(killed=[ec])
    rows = format.discovery_killed_rows(report, {})
    assert rows[0]["title"] == "—" and rows[0]["price_usd"] is None
    assert rows[0]["kill_rule"] == "K1"
