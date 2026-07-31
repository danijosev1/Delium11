"""Test helpers for the review providers and review ingestion: fake GET/POST
transports and builders for Unwrangle and Apify response bodies. No network."""

from __future__ import annotations

from typing import Any

from delium.providers.base import HttpResult

ASIN = "B08EXAMPLE"


def unwrangle_body(n: int = 3, *, asin: str = ASIN, success: bool = True) -> dict[str, Any]:
    star_cycle = [5, 3, 1, 4, 2]
    reviews = [
        {
            "id": f"R{i}",
            "title": f"Review {i}",
            "review": f"body text {i}",
            "rating": star_cycle[i % len(star_cycle)],
            "verified_purchase": i % 2 == 0,
            "date": f"2026-07-{i + 1:02d}",
            "helpful_votes": i,
            "author": f"user{i}",
        }
        for i in range(n)
    ]
    body: dict[str, Any] = {
        "success": success,
        "asin": asin,
        "total_results": 100,
        "reviews": reviews,
    }
    if not success:
        body["error"] = "blocked"
        body["reviews"] = []
    return body


def apify_items(n: int = 2, *, asin: str = ASIN) -> list[dict[str, Any]]:
    star_cycle = [4, 2, 5, 1, 3]
    return [
        {
            "reviewId": f"A{i}",
            "reviewTitle": f"Apify review {i}",
            "reviewDescription": f"apify body {i}",
            "ratingScore": star_cycle[i % len(star_cycle)],
            "isVerified": True,
            "date": f"2026-06-{i + 1:02d}",
            "reviewReaction": f"{i} people found this helpful",
            "reviewerName": f"user{i}",
        }
        for i in range(n)
    ]


def get_resp(body: Any, status: int = 200) -> HttpResult:
    return HttpResult(status=status, body=body)


def http(code: int, body: Any = None) -> HttpResult:
    return HttpResult(status=code, body=body if body is not None else {})


class FakeGetTransport:
    """request_json transport; queued results repeat the last for extra calls."""

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


class FakePostTransport:
    """post_json transport; body may be a dict or a list (Apify)."""

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.calls: list[Any] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def post_json(self, url: str, body: Any, headers: dict[str, str]) -> HttpResult:
        self.calls.append(body)
        index = min(len(self.calls) - 1, len(self._results) - 1)
        item = self._results[index]
        if isinstance(item, Exception):
            raise item
        return item
