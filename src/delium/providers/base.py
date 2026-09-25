"""Shared provider primitives: error hierarchy and an injectable HTTP transport.

Keeping HTTP behind a small `Transport` protocol means adapters contain only
provider logic (params, parsing, throttling) and tests inject a fake transport
instead of patching the network. The default transport uses the stdlib so the
project takes on no HTTP dependency.
"""

from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

# Sent on every request so servers may compress; the response is decompressed
# from its Content-Encoding header before JSON parsing. Some providers (Keepa)
# gzip responses regardless — decoding is keyed off the response header, not this.
_ACCEPT_ENCODING = "gzip, deflate"


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
        request = urllib.request.Request(
            f"{url}?{query}", method="GET", headers={"Accept-Encoding": _ACCEPT_ENCODING}
        )
        return self._send(request)

    def post_json(self, url: str, body: Any, headers: Mapping[str, str]) -> HttpResult:
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept-Encoding": _ACCEPT_ENCODING,
                **dict(headers),
            },
        )
        return self._send(request)

    def _send(self, request: urllib.request.Request) -> HttpResult:
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = _decode_body(response.read(), _content_encoding(response))
                return HttpResult(status=response.status, body=_load_json(raw))
        except urllib.error.HTTPError as exc:
            raw = _decode_body(exc.read(), _content_encoding(exc))
            return HttpResult(status=exc.code, body=_load_json(raw))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderNetworkError(str(exc)) from exc


def _content_encoding(response: Any) -> str | None:
    """The response's Content-Encoding header, tolerant of a response object with
    no headers or a None headers container (e.g. `HTTPError(hdrs=None)`)."""
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get("Content-Encoding")
    except AttributeError:
        return None
    return value if isinstance(value, str) else None


def _decode_body(raw: bytes, content_encoding: str | None) -> bytes:
    """Decompress a response body per its Content-Encoding. urllib does NOT
    auto-decompress, and some providers (Keepa) always gzip — without this the
    raw gzip bytes reach the JSON parser and fail silently. On a malformed
    compressed body the raw bytes are returned (the JSON parser then rejects
    them) rather than raising a misleading network error."""
    if not raw or not content_encoding:
        return raw
    encoding = content_encoding.strip().lower()
    try:
        if encoding == "gzip":
            return gzip.decompress(raw)
        if encoding == "deflate":
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)  # raw deflate (no zlib header)
    except (OSError, EOFError, zlib.error):
        return raw
    return raw


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
