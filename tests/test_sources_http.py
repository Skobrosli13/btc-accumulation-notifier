"""HTTP helpers: retry/backoff, 429 Retry-After handling, fail-soft, post_json."""
from __future__ import annotations

import pytest
import requests

from app.sources import _http


class _Resp:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.headers = headers or {}

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Make backoff/Retry-After sleeps instant so the suite stays fast, and
    record how long the code asked to wait."""
    waits = []
    monkeypatch.setattr(_http.time, "sleep", lambda s: waits.append(s))
    # Deterministic jitter: full backoff each time.
    monkeypatch.setattr(_http.random, "uniform", lambda a, b: b)
    return waits


def _seq_request(monkeypatch, responses):
    """Patch requests.request to return/raise from ``responses`` in order."""
    calls = {"n": 0}

    def fake(method, url, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        item = responses[min(i, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(_http.requests, "request", fake)
    return calls


def test_get_json_succeeds_first_try(monkeypatch):
    calls = _seq_request(monkeypatch, [_Resp(200, {"ok": True})])
    assert _http.get_json("http://x") == {"ok": True}
    assert calls["n"] == 1


def test_get_json_retries_on_timeout_then_succeeds(monkeypatch, _no_sleep):
    calls = _seq_request(monkeypatch, [
        requests.Timeout("slow"),
        _Resp(200, {"ok": 1}),
    ])
    assert _http.get_json("http://x") == {"ok": 1}
    assert calls["n"] == 2
    assert len(_no_sleep) == 1  # one backoff between the two attempts


def test_get_json_retries_on_5xx_then_succeeds(monkeypatch):
    calls = _seq_request(monkeypatch, [
        _Resp(503),
        _Resp(200, {"ok": 2}),
    ])
    assert _http.get_json("http://x") == {"ok": 2}
    assert calls["n"] == 2


def test_get_json_exhausts_retries_returns_none(monkeypatch):
    calls = _seq_request(monkeypatch, [requests.ConnectionError("down")])
    assert _http.get_json("http://x") is None
    # _MAX_ATTEMPTS attempts, all failing.
    assert calls["n"] == _http._MAX_ATTEMPTS


def test_get_json_non_retryable_4xx_no_retry(monkeypatch):
    calls = _seq_request(monkeypatch, [_Resp(404)])
    assert _http.get_json("http://x") is None
    assert calls["n"] == 1  # 404 is not retried


def test_429_honors_retry_after_capped(monkeypatch, _no_sleep):
    calls = _seq_request(monkeypatch, [
        _Resp(429, headers={"Retry-After": "100"}),  # asks 100s; we cap it
        _Resp(200, {"ok": 3}),
    ])
    assert _http.get_json("http://x") == {"ok": 3}
    assert calls["n"] == 2
    # The honored wait equals the cap, not 100s, so a run can't hang.
    assert _no_sleep == [_http._MAX_RETRY_AFTER]


def test_429_without_retry_after_uses_backoff(monkeypatch, _no_sleep):
    _seq_request(monkeypatch, [_Resp(429), _Resp(200, {"ok": 4})])
    assert _http.get_json("http://x") == {"ok": 4}
    assert len(_no_sleep) == 1
    assert _no_sleep[0] <= _http._MAX_BACKOFF


def test_get_json_bad_json_returns_none(monkeypatch):
    _seq_request(monkeypatch, [_Resp(200, ValueError("bad json"))])
    assert _http.get_json("http://x") is None


def test_get_text_returns_body(monkeypatch):
    _seq_request(monkeypatch, [_Resp(200, text="hello")])
    assert _http.get_text("http://x") == "hello"


def test_post_json_sends_body_and_returns(monkeypatch):
    captured = {}

    def fake(method, url, **kwargs):
        captured["method"] = method
        captured["json"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers")
        return _Resp(200, {"posted": True})

    monkeypatch.setattr(_http.requests, "request", fake)
    out = _http.post_json("http://x", json_body={"type": "us-btc-spot"},
                          headers={"x-soso-api-key": "k"})
    assert out == {"posted": True}
    assert captured["method"] == "POST"
    assert captured["json"] == {"type": "us-btc-spot"}
    assert captured["headers"]["x-soso-api-key"] == "k"


def test_post_json_fails_soft(monkeypatch):
    _seq_request(monkeypatch, [requests.ConnectionError("down")])
    assert _http.post_json("http://x", json_body={}) is None
