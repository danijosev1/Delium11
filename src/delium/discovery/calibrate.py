"""Calibration orchestration — read stored products, run the pure calibration
harness, and (only with an explicit, confirmed refresh) re-fetch Keepa first.

Stored-data-only by default: no network. `refresh_products` performs a batched,
cache-first Keepa hydration and is called by the CLI/UI only after the user
confirms the token estimate.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from delium.analysis.calibration import (
    CalibrationReport,
    ProductCalibration,
    build_report,
    compare_fee,
    compare_units,
    delium_fee_estimate,
    delium_unit_estimate,
)
from delium.analysis.demand import load_velocity_curves
from delium.analysis.models import Dimensions
from delium.config.models import DeliumConfig
from delium.database import repository


def _dims(row: sqlite3.Row) -> Dimensions | None:
    raw = row["dims_json"]
    if not raw:
        return None
    parsed = json.loads(raw)
    keys = ("length_mm", "width_mm", "height_mm")
    if not all(k in parsed for k in keys):
        return None
    return Dimensions(*(int(parsed[k]) for k in keys))


def _latest_bsr(conn: sqlite3.Connection, asin: str) -> int | None:
    for r in reversed(repository.get_price_bsr_history(conn, asin)):
        if r["bsr"] is not None:
            return int(r["bsr"])
    return None


def _row_get(row: sqlite3.Row, column: str) -> Any:
    try:
        return row[column]
    except (IndexError, KeyError):
        return None


def collect_rows(
    conn: sqlite3.Connection, asins: list[str], marketplace: str
) -> list[ProductCalibration]:
    """Build a per-product calibration comparison from stored data only. ASINs
    with no stored product row, or no Keepa monthlySold/fee to compare against,
    are skipped (nothing to calibrate)."""
    curves = load_velocity_curves()
    out: list[ProductCalibration] = []
    for asin in dict.fromkeys(a.strip().upper() for a in asins if a.strip()):
        row = repository.get_product(conn, asin, marketplace)
        if row is None:
            continue
        keepa_units = _int(_row_get(row, "monthly_sold"))
        keepa_fee = _int(_row_get(row, "fba_pick_pack_cents"))
        if keepa_units is None and keepa_fee is None:
            continue  # no Keepa ground truth stored for this product
        category = row["category_path"]
        delium_units = delium_unit_estimate(curves, category, _latest_bsr(conn, asin))
        delium_fee = delium_fee_estimate(category, _dims(row), row["weight_g"])
        out.append(
            ProductCalibration(
                asin=asin,
                category=category,
                units=compare_units(delium_units, keepa_units),
                fee=compare_fee(delium_fee, keepa_fee),
            )
        )
    return out


def _int(value: object | None) -> int | None:
    return int(value) if isinstance(value, int) else None


def all_stored_asins(conn: sqlite3.Connection, marketplace: str) -> list[str]:
    return [r["asin"] for r in repository.get_products_by_marketplace(conn, marketplace)]


def run(
    conn: sqlite3.Connection, *, asins: list[str] | None, marketplace: str
) -> CalibrationReport:
    """Run calibration over the given ASINs (or every stored product when None).
    Stored data only — no network."""
    targets = asins if asins else all_stored_asins(conn, marketplace)
    return build_report(collect_rows(conn, targets, marketplace))


def refresh_products(
    asins: list[str],
    marketplace: str,
    config: DeliumConfig,
    *,
    keepa_client_factory: object,
    run_id: str,
) -> int:
    """Batched, cache-first Keepa re-fetch for the given ASINs before calibrating.
    PAID — callers confirm the token estimate first. Returns tokens used."""
    from delium.ingestion import hydrate_products

    factory = keepa_client_factory
    if factory is None or not asins:
        return 0
    client = factory(marketplace)  # type: ignore[operator]
    hydrate_products(
        [a.strip().upper() for a in asins],
        run_id=run_id,
        client=client,
        config=config,
        force=True,
    )
    from delium.database.connection import get_connection

    with get_connection() as conn:
        return repository.run_token_total(conn, run_id)
