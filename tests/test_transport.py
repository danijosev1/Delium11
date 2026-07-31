"""Tests for the default urllib transport and JSON body handling — exercised
without real network via monkeypatched urlopen."""

from __future__ import annotations

import io
import urllib.error
from typing import Any

import pytest

from delium.providers.base import ProviderNetworkError, UrllibTransport, _load_json


def test_load_json_variants() -> None:
    assert _load_json(b"") == {}
    assert _load_json(b"not json") == {}
    assert _load_json(b"42") == {}  # scalar JSON is ignored
    assert _load_json(b"[1, 2, 3]") == [1, 2, 3]  # arrays pass through (Apify)
    assert _load_json(b'{"a": 1}') == {"a": 1}


class _FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def test_transport_returns_parsed_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(_request: Any, timeout: float = 0) -> _FakeResponse:
        return _FakeResponse(b'{"ok": true}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = UrllibTransport().request_json("https://example.test", {"a": "b"})
    assert result.status == 200
    assert result.body == {"ok": True}


def test_transport_maps_http_error_to_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(_request: Any, timeout: float = 0) -> Any:
        raise urllib.error.HTTPError(
            url="https://example.test",
            code=429,
            msg="Too Many Requests",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"tokensLeft": 0}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = UrllibTransport().request_json("https://example.test", {})
    assert result.status == 429
    assert result.body == {"tokensLeft": 0}


def test_transport_raises_on_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(_request: Any, timeout: float = 0) -> Any:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(ProviderNetworkError):
        UrllibTransport().request_json("https://example.test", {})
