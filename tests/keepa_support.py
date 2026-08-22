"""Shared test helpers for the Keepa adapter and product ingestion.

Provides a fake HTTP transport and a builder for realistic Keepa `/product`
response bodies, so tests never touch the network.
"""

from __future__ import annotations

from typing import Any

from delium.providers.base import HttpResult

# Keepa minutes for three consecutive days (verified against the epoch math):
#   2025-01-01, 2025-01-02, 2025-01-03  (UTC midnight)
KM_DAY1 = 7364160
KM_DAY2 = KM_DAY1 + 1440
KM_DAY3 = KM_DAY2 + 1440

DEFAULT_ASIN = "B08EXAMPLE"


def keepa_product_body(
    asin: str = DEFAULT_ASIN,
    *,
    found: bool = True,
    tokens_left: int = 300,
    refill_rate: int = 20,
    tokens_consumed: int = 3,
) -> dict[str, Any]:
    """A realistic Keepa `/product` response body."""
    envelope: dict[str, Any] = {
        "tokensLeft": tokens_left,
        "refillRate": refill_rate,
        "tokensConsumed": tokens_consumed,
    }
    if not found:
        envelope["products"] = []
        return envelope

    csv: list[Any] = [None] * 18
    csv[0] = [KM_DAY1, 2199, KM_DAY2, 2099, KM_DAY3, -1]  # Amazon price (cents), -1 = gap
    csv[3] = [KM_DAY1, 1500, KM_DAY2, 1600]  # BSR
    csv[16] = [KM_DAY1, 45]  # rating 4.5 (0-50 scale)
    csv[17] = [KM_DAY1, 480]  # review count

    envelope["products"] = [
        {
            "asin": asin,
            "title": "Test Silicone Tray",
            "brand": "Acme",
            "manufacturer": "Acme Corp",
            "eanList": ["0012345678905"],
            "upcList": ["012345678905"],
            "categoryTree": [
                {"catId": 1, "name": "Baby"},
                {"catId": 2, "name": "Feeding"},
            ],
            "packageLength": 200,
            "packageWidth": 150,
            "packageHeight": 40,
            "packageWeight": 300,
            "imagesCSV": "a.jpg,b.jpg,c.jpg",
            "csv": csv,
        }
    ]
    return envelope


def ok(body: dict[str, Any]) -> HttpResult:
    return HttpResult(status=200, body=body)


def http(code: int, body: dict[str, Any] | None = None) -> HttpResult:
    return HttpResult(status=code, body=body or {})


class FakeTransport:
    """Returns queued results in order; the last result repeats for extra calls.

    A queued item may be an `HttpResult` (returned) or an `Exception` (raised) —
    the latter simulates transport-level network failures.
    """

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.calls: list[dict[str, str]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def request_json(self, url: str, params: dict[str, str]) -> HttpResult:
        self.calls.append(dict(params))
        index = min(len(self.calls) - 1, len(self._results) - 1)
        item = self._results[index]
        if isinstance(item, Exception):
            raise item
        return item
