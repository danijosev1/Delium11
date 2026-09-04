"""Insert/read roundtrips through the repository helpers."""

from __future__ import annotations

import json
from pathlib import Path

from delium.database import get_connection, repository


def test_run_and_raw_fetch_roundtrip(initialized_db: Path) -> None:
    with get_connection() as conn:
        run_id = repository.insert_run(
            conn, command="validate", input_="B0X", config_snapshot={"weights": {"demand": 25}}
        )
        fetch_id = repository.insert_raw_fetch(
            conn,
            run_id=run_id,
            provider="keepa",
            endpoint="product",
            request_key="keepa:product:B0X",
            payload={"asin": "B0X", "bsr": 1200},
            cost_usd=0.0,
            tokens_used=3,
        )

    with get_connection() as conn:
        run = repository.get_run(conn, run_id)
        fetch = repository.latest_raw_fetch(conn, "keepa", "keepa:product:B0X")

    assert run is not None
    assert run["command"] == "validate"
    assert json.loads(run["config_snapshot"]) == {"weights": {"demand": 25}}
    assert fetch is not None
    assert fetch["id"] == fetch_id
    assert json.loads(fetch["payload"])["bsr"] == 1200


def test_latest_raw_fetch_returns_newest(initialized_db: Path) -> None:
    with get_connection() as conn:
        run_id = repository.insert_run(conn, command="validate")
        repository.insert_raw_fetch(
            conn,
            run_id=run_id,
            provider="keepa",
            endpoint="product",
            request_key="keepa:product:B0X",
            payload={"v": 1},
        )
        second = repository.insert_raw_fetch(
            conn,
            run_id=run_id,
            provider="keepa",
            endpoint="product",
            request_key="keepa:product:B0X",
            payload={"v": 2},
        )

    with get_connection() as conn:
        latest = repository.latest_raw_fetch(conn, "keepa", "keepa:product:B0X")

    assert latest is not None
    assert latest["id"] == second
    assert json.loads(latest["payload"])["v"] == 2


def test_finish_run_updates_status_and_costs(initialized_db: Path) -> None:
    with get_connection() as conn:
        run_id = repository.insert_run(conn, command="validate")
        repository.finish_run(
            conn, run_id, status="complete", data_cost_usd=1.42, llm_cost_usd=0.31
        )

    with get_connection() as conn:
        run = repository.get_run(conn, run_id)

    assert run is not None
    assert run["status"] == "complete"
    assert run["data_cost_usd"] == 1.42
    assert run["finished_at"] is not None


def test_product_upsert_and_derived_roundtrip(initialized_db: Path) -> None:
    with get_connection() as conn:
        run_id = repository.insert_run(conn, command="validate")
        fetch_id = repository.insert_raw_fetch(
            conn,
            run_id=run_id,
            provider="keepa",
            endpoint="product",
            request_key="keepa:product:B0X",
            payload={},
        )
        repository.upsert_product(
            conn,
            asin="B0X",
            fetch_id=fetch_id,
            title="Silicone Tray",
            brand="Acme",
            dims={"length_mm": 200, "width_mm": 150, "height_mm": 40},
            weight_g=300,
            size_tier="large-standard",
            images_count=7,
            amazon_on_listing=True,
        )
        repository.upsert_product_derived(
            conn,
            asin="B0X",
            fetch_id=fetch_id,
            est_units_low=300,
            est_units_high=800,
            history_days=180,
        )

    with get_connection() as conn:
        product = repository.get_product(conn, "B0X")
        derived = repository.get_product_derived(conn, "B0X")

    assert product is not None
    assert product["title"] == "Silicone Tray"
    assert product["amazon_on_listing"] == 1
    assert json.loads(product["dims_json"])["length_mm"] == 200
    assert derived is not None
    assert derived["est_units_low"] == 300
    assert derived["history_days"] == 180


def test_product_upsert_overwrites(initialized_db: Path) -> None:
    with get_connection() as conn:
        run_id = repository.insert_run(conn, command="validate")
        fetch_id = repository.insert_raw_fetch(
            conn,
            run_id=run_id,
            provider="keepa",
            endpoint="product",
            request_key="k",
            payload={},
        )
        repository.upsert_product(conn, asin="B0X", fetch_id=fetch_id, title="Old")
        repository.upsert_product(conn, asin="B0X", fetch_id=fetch_id, title="New")

    with get_connection() as conn:
        product = repository.get_product(conn, "B0X")
        count = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]

    assert product is not None
    assert product["title"] == "New"
    assert count == 1


def test_price_history_roundtrip_and_ordering(initialized_db: Path) -> None:
    with get_connection() as conn:
        run_id = repository.insert_run(conn, command="validate")
        fetch_id = repository.insert_raw_fetch(
            conn,
            run_id=run_id,
            provider="keepa",
            endpoint="product",
            request_key="k",
            payload={},
        )
        repository.upsert_product(conn, asin="B0X", fetch_id=fetch_id)
        repository.upsert_price_bsr_history(
            conn, asin="B0X", captured_on="2026-07-02", price_cents=2199, bsr=1500
        )
        repository.upsert_price_bsr_history(
            conn, asin="B0X", captured_on="2026-07-01", price_cents=2099, bsr=1600
        )
        # Same date upserts, not duplicates.
        repository.upsert_price_bsr_history(
            conn, asin="B0X", captured_on="2026-07-01", price_cents=1999, bsr=1550
        )

    with get_connection() as conn:
        history = repository.get_price_bsr_history(conn, "B0X")

    assert [h["captured_on"] for h in history] == ["2026-07-01", "2026-07-02"]
    assert history[0]["price_cents"] == 1999  # upserted value


def test_keyword_and_serp_roundtrip(initialized_db: Path) -> None:
    with get_connection() as conn:
        run_id = repository.insert_run(conn, command="discover")
        fetch_id = repository.insert_raw_fetch(
            conn,
            run_id=run_id,
            provider="dataforseo",
            endpoint="bulk_search_volume",
            request_key="kw",
            payload={},
        )
        repository.upsert_keyword(
            conn,
            phrase="silicone baby food tray",
            fetch_id=fetch_id,
            volume=9400,
            volume_series=[800, 810, 900],
            cpc_cents=45,
        )
        repository.upsert_serp_ranking(
            conn,
            keyword_phrase="silicone baby food tray",
            asin="B0AAA",
            position=1,
            captured_on="2026-07-01",
        )
        repository.upsert_serp_ranking(
            conn,
            keyword_phrase="silicone baby food tray",
            asin="B0BBB",
            position=2,
            captured_on="2026-07-01",
            sponsored=True,
        )

    with get_connection() as conn:
        keyword = repository.get_keyword(conn, "silicone baby food tray")
        serps = repository.get_serp_rankings(conn, "silicone baby food tray")

    assert keyword is not None
    assert keyword["volume"] == 9400
    assert json.loads(keyword["volume_series"]) == [800, 810, 900]
    assert [s["asin"] for s in serps] == ["B0AAA", "B0BBB"]
    assert serps[1]["sponsored"] == 1


def test_competitor_set_roundtrip(initialized_db: Path) -> None:
    with get_connection() as conn:
        run_id = repository.insert_run(conn, command="validate")
        set_id = repository.insert_competitor_set(
            conn,
            run_id=run_id,
            target_asin="B0X",
            member_asins=["B0AAA", "B0BBB", "B0CCC"],
            selection_method="serp_top",
        )

    with get_connection() as conn:
        cset = repository.get_competitor_set(conn, set_id)

    assert cset is not None
    assert cset["target_asin"] == "B0X"
    assert json.loads(cset["member_asins"]) == ["B0AAA", "B0BBB", "B0CCC"]


def test_reviews_and_themes_roundtrip(initialized_db: Path) -> None:
    with get_connection() as conn:
        run_id = repository.insert_run(conn, command="validate")
        fetch_id = repository.insert_raw_fetch(
            conn,
            run_id=run_id,
            provider="reviews",
            endpoint="by_asin",
            request_key="rev",
            payload={},
        )
        repository.upsert_product(conn, asin="B0X", fetch_id=fetch_id)
        for i, stars in enumerate([1, 2, 5]):
            repository.insert_review(
                conn,
                review_id=f"r{i}",
                asin="B0X",
                fetch_id=fetch_id,
                stars=stars,
                title=f"review {i}",
                body="lid cracked after a week",
                verified=True,
                review_date=f"2026-06-0{i + 1}",
            )
        theme_id = repository.insert_review_theme(
            conn,
            run_id=run_id,
            asin="B0X",
            kind="complaint",
            theme="lid cracks",
            quote_review_ids=["r0", "r1", "r2"],
            frequency_pct=22.0,
            severity=3,
        )

    with get_connection() as conn:
        reviews = repository.get_reviews_for_asin(conn, "B0X")
        themes = repository.get_review_themes(conn, "B0X")

    assert len(reviews) == 3
    assert {r["stars"] for r in reviews} == {1, 2, 5}
    assert len(themes) == 1
    assert themes[0]["id"] == theme_id
    assert themes[0]["severity"] == 3
    assert json.loads(themes[0]["quote_review_ids"]) == ["r0", "r1", "r2"]


def test_agent_runs_roundtrip(initialized_db: Path) -> None:
    with get_connection() as conn:
        run_id = repository.insert_run(conn, command="validate", input_="B0X")
        repository.upsert_product(
            conn,
            asin="B0X",
            fetch_id=repository.insert_raw_fetch(
                conn,
                run_id=run_id,
                provider="keepa",
                endpoint="product",
                request_key="keepa:product:US:B0X",
                payload={},
            ),
        )
        repository.insert_agent_run(
            conn,
            run_id=run_id,
            asin="B0X",
            marketplace="US",
            agent="review_miner",
            status="degraded",
            model="claude-haiku-4-5",
            provider="anthropic",
            cost_usd=0.0075,
            tokens_in=1000,
            tokens_out=300,
            output={"complaints": []},
            error=None,
        )
        repository.insert_agent_run(
            conn,
            run_id=run_id,
            asin="B0X",
            marketplace="US",
            agent="strategist",
            status="ok",
            model="claude-sonnet-5",
            provider="anthropic",
            cost_usd=0.02,
        )
    with get_connection() as conn:
        runs = repository.get_agent_runs(conn, "B0X", "US")
        latest_miner = repository.get_latest_agent_run(
            conn, asin="B0X", marketplace="US", agent="review_miner"
        )
    assert len(runs) == 2
    assert latest_miner is not None
    assert latest_miner["status"] == "degraded"
    assert latest_miner["cost_usd"] == 0.0075
    assert json.loads(latest_miner["output"]) == {"complaints": []}


def test_review_theme_structured_columns_roundtrip(initialized_db: Path) -> None:
    with get_connection() as conn:
        run_id = repository.insert_run(conn, command="validate", input_="B0Y")
        repository.upsert_product(
            conn,
            asin="B0Y",
            fetch_id=repository.insert_raw_fetch(
                conn,
                run_id=run_id,
                provider="keepa",
                endpoint="product",
                request_key="keepa:product:US:B0Y",
                payload={},
            ),
        )
        repository.insert_review_theme(
            conn,
            run_id=run_id,
            asin="B0Y",
            kind="complaint",
            theme="leaks",
            quote_review_ids=["r0", "r1", "r2"],
            addressability="fixable",
            cogs_delta=0.10,
            category="packaging",
        )
        fid = repository.insert_feature_request(
            conn,
            run_id=run_id,
            asin="B0Y",
            feature="lid",
            supporting_review_ids=["r0", "r1", "r2"],
            absent_from_competitors=None,
        )
        bid = repository.insert_bundle_signal(
            conn,
            run_id=run_id,
            asin="B0Y",
            complement="bag",
            supporting_review_ids=["r0", "r1", "r2"],
        )
    with get_connection() as conn:
        theme = repository.get_review_themes(conn, "B0Y")[0]
        feature = repository.get_feature_requests(conn, "B0Y")[0]
        bundle = repository.get_bundle_signals(conn, "B0Y")[0]
    assert theme["addressability"] == "fixable" and theme["cogs_delta"] == 0.10
    assert theme["category"] == "packaging"
    assert feature["id"] == fid and feature["absent_from_competitors"] is None
    assert bundle["id"] == bid and bundle["complement"] == "bag"
