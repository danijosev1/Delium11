"""Test helpers for the DataForSEO adapter and keyword ingestion: a fake POST
transport and builders for realistic response bodies. No network access."""

from __future__ import annotations

from typing import Any

from delium.providers.base import HttpResult

SEED = "silicone baby food tray"


def _envelope(items: list[dict[str, Any]], cost: float) -> dict[str, Any]:
    return {
        "status_code": 20000,
        "cost": cost,
        "tasks": [
            {
                "status_code": 20000,
                "cost": cost,
                "result": [{"items": items}],
            }
        ],
    }


def volume_body(volume: int = 9400, *, seed: str = SEED, cost: float = 0.024) -> dict[str, Any]:
    return _envelope([{"keyword": seed, "search_volume": volume}], cost)


def related_body(
    phrases: list[tuple[str, int]] | None = None, *, cost: float = 0.02
) -> dict[str, Any]:
    phrases = phrases or [
        ("baby food storage containers", 5400),
        ("freezer tray silicone", 3200),
        ("baby food freezer molds", 1800),
    ]
    items = [
        {"keyword_data": {"keyword": k, "keyword_info": {"search_volume": v}}} for k, v in phrases
    ]
    return _envelope(items, cost)


def serp_body(*, cost: float = 0.006) -> dict[str, Any]:
    items = [
        {
            "type": "amazon_serp",
            "rank_absolute": 1,
            "data_asin": "B0AAA00001",
            "title": "Tray A",
            "price_from": 21.99,
        },
        {
            "type": "amazon_paid",
            "rank_absolute": 2,
            "data_asin": "B0BBB00002",
            "title": "Tray B",
            "price_from": 19.99,
        },
        {"type": "amazon_serp", "rank_absolute": 3, "data_asin": "B0CCC00003", "title": "Tray C"},
        {"type": "related_searches", "rank_absolute": 4},  # no ASIN → skipped
        {
            "type": "editorial_recommendations",
            "rank_absolute": 5,
            "data_asin": "B0DDD",
        },  # wrong type → skipped
    ]
    return _envelope(items, cost)


def task_error_body(status: int = 40501) -> dict[str, Any]:
    return {
        "status_code": 20000,
        "cost": 0.0,
        "tasks": [{"status_code": status, "status_message": "bad request", "result": None}],
    }


def ok(body: dict[str, Any]) -> HttpResult:
    return HttpResult(status=200, body=body)


def http(code: int, body: dict[str, Any] | None = None) -> HttpResult:
    return HttpResult(status=code, body=body or {})


class FakePostTransport:
    """Returns queued results in order; the last repeats for extra calls.
    A queued Exception is raised (simulating a network failure)."""

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, Any]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def post_json(self, url: str, body: Any, headers: dict[str, str]) -> HttpResult:
        self.calls.append((url, body))
        index = min(len(self.calls) - 1, len(self._results) - 1)
        item = self._results[index]
        if isinstance(item, Exception):
            raise item
        return item
