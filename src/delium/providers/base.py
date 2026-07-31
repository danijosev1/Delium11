"""Shared provider primitives: error hierarchy and an injectable HTTP transport.

Keeping HTTP behind a small `Transport` protocol means adapters contain only
provider logic (params, parsing, throttling) and tests inject a fake transport
instead of patching the network. The default transport uses the stdlib so the
project takes on no HTTP dependency.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class ProviderError(Exception):
    """Base class for all provider-layer failures."""


class ProviderConfigError(ProviderError):
    """Missing/invalid configuration, e.g. absent API key."""


class ProviderAuthError(ProviderError):
    """Authentication rejected (HTTP 401/403)."""


class ProviderRateLimitError(ProviderError):
    """Rate/token limit could not be satisfied within the allowed wait."""


class ProviderNetworkError(ProviderError):
    """Transport-level failure (connection/timeout) — retryable."""


class ProviderResponseError(ProviderError):
    """Unexpected HTTP status or unparseable body."""


@dataclass(frozen=True)
class HttpResult:
    status: int
    # Parsed JSON — usually an object, but some providers (Apify dataset items)
    # return a top-level array, so this is intentionally `Any`.
    body: Any


class Transport(Protocol):
    """Minimal HTTP surface a GET-based adapter needs (Keepa)."""

    def request_json(self, url: str, params: Mapping[str, str]) -> HttpResult: ...


class PostTransport(Protocol):
    """HTTP surface a POST+JSON adapter needs (DataForSEO)."""

    def post_json(self, url: str, body: Any, headers: Mapping[str, str]) -> HttpResult: ...


class UrllibTransport:
    """Default transport backed by urllib. Raises `ProviderNetworkError` on
    connection failures so the adapter's retry loop can handle them; HTTP error
    statuses are returned (with any JSON body) rather than raised."""

    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout

    def request_json(self, url: str, params: Mapping[str, str]) -> HttpResult:
        query = urllib.parse.urlencode(dict(params))
        request = urllib.request.Request(f"{url}?{query}", method="GET")
        return self._send(request)

    def post_json(self, url: str, body: Any, headers: Mapping[str, str]) -> HttpResult:
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", **dict(headers)},
        )
        return self._send(request)

    def _send(self, request: urllib.request.Request) -> HttpResult:
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return HttpResult(status=response.status, body=_load_json(response.read()))
        except urllib.error.HTTPError as exc:
            return HttpResult(status=exc.code, body=_load_json(exc.read()))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderNetworkError(str(exc)) from exc


def _load_json(raw: bytes) -> Any:
    """Parse a JSON body, returning {} on empty/invalid input. A valid array
    (e.g. Apify dataset items) is returned as-is."""
    if not raw:
        return {}
    try:
        parsed: Any = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict | list) else {}
