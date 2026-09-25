"""Transport-level tests for gzip/deflate response decoding.

The real bug: `UrllibTransport` read the raw response bytes without decompressing
`Content-Encoding`, so a gzip-compressed body (Keepa always gzips) reached the
JSON parser and failed silently → `{}`. Unit tests never caught it because the
fake transports return already-parsed `HttpResult`s, bypassing HTTP entirely.

These tests drive the REAL `UrllibTransport` with a monkeypatched `urlopen`, so
they exercise the decode path for every provider that shares it (Keepa GET,
DataForSEO/review POST).
"""

from __future__ import annotations

import email.message
import gzip
import io
import json
import urllib.error
import urllib.request
import zlib
from typing import Any

import pytest

from delium.providers.base import ProviderNetworkError, UrllibTransport

_PAYLOAD = {"products": [{"asin": "B0F543R23M"}], "tokensLeft": 1200}


class _FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200, content_encoding: str | None) -> None:
        self._body = body
        self.status = status
        self.headers = email.message.Message()
        if content_encoding is not None:
            self.headers["Content-Encoding"] = content_encoding

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _patch_urlopen(monkeypatch: pytest.MonkeyPatch, response: Any) -> list[urllib.request.Request]:
    """Capture the Request(s) and return the given fake response (or raise it)."""
    seen: list[urllib.request.Request] = []

    def fake(req: urllib.request.Request, timeout: float | None = None) -> Any:
        seen.append(req)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return seen


def _gzip(obj: Any) -> bytes:
    return gzip.compress(json.dumps(obj).encode("utf-8"))


def test_get_decodes_gzip_and_advertises_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _patch_urlopen(monkeypatch, _FakeResponse(_gzip(_PAYLOAD), content_encoding="gzip"))
    result = UrllibTransport().request_json("https://api.keepa.com/product", {"asin": "x"})
    assert result.status == 200
    assert result.body == _PAYLOAD  # decoded, not {}
    assert seen[0].get_header("Accept-encoding") == "gzip, deflate"


def test_post_decodes_gzip(monkeypatch: pytest.MonkeyPatch) -> None:
    # The same transport backs DataForSEO + review POSTs — prove POST decodes too.
    _patch_urlopen(monkeypatch, _FakeResponse(_gzip({"tasks": [1]}), content_encoding="gzip"))
    result = UrllibTransport().post_json("https://api.dataforseo.com/x", {"q": 1}, {})
    assert result.body == {"tasks": [1]}


def test_decodes_zlib_deflate(monkeypatch: pytest.MonkeyPatch) -> None:
    body = zlib.compress(json.dumps(_PAYLOAD).encode("utf-8"))
    _patch_urlopen(monkeypatch, _FakeResponse(body, content_encoding="deflate"))
    result = UrllibTransport().request_json("https://x", {})
    assert result.body == _PAYLOAD


def test_decodes_raw_deflate(monkeypatch: pytest.MonkeyPatch) -> None:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)  # raw deflate, no zlib header
    body = compressor.compress(json.dumps(_PAYLOAD).encode("utf-8")) + compressor.flush()
    _patch_urlopen(monkeypatch, _FakeResponse(body, content_encoding="deflate"))
    result = UrllibTransport().request_json("https://x", {})
    assert result.body == _PAYLOAD


def test_identity_body_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps(_PAYLOAD).encode("utf-8")
    _patch_urlopen(monkeypatch, _FakeResponse(body, content_encoding=None))
    result = UrllibTransport().request_json("https://x", {})
    assert result.body == _PAYLOAD


def test_malformed_gzip_degrades_to_empty_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    # Header claims gzip but the bytes are not gzip → return raw → JSON parse
    # yields {} rather than raising a misleading network error.
    _patch_urlopen(monkeypatch, _FakeResponse(b"not gzip", content_encoding="gzip"))
    result = UrllibTransport().request_json("https://x", {})
    assert result.body == {}


def test_http_error_body_is_gzip_decoded(monkeypatch: pytest.MonkeyPatch) -> None:
    hdrs = email.message.Message()
    hdrs["Content-Encoding"] = "gzip"
    err = urllib.error.HTTPError(
        "https://x", 429, "Too Many Requests", hdrs, io.BytesIO(_gzip({"error": "rate"}))
    )
    _patch_urlopen(monkeypatch, err)
    result = UrllibTransport().request_json("https://x", {})
    assert result.status == 429
    assert result.body == {"error": "rate"}


def test_network_error_still_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(monkeypatch, urllib.error.URLError("boom"))
    with pytest.raises(ProviderNetworkError):
        UrllibTransport().request_json("https://x", {})
