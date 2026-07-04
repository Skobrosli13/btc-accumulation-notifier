"""Sharadar adapter: pure datatable mapping + cursor pagination + secret hygiene.

The default suite is offline (pagination is exercised against canned pages). A
live smoke test runs only when RUN_LIVE_SHARADAR is set (and a key is present).
"""
from __future__ import annotations

import os

import pytest

from app.data.equities import sharadar


def test_datatable_rows_maps_columns_to_dicts():
    payload = {"datatable": {"columns": [{"name": "ticker"}, {"name": "v"}],
                             "data": [["AAPL", 1], ["MSFT", 2]]}}
    assert sharadar.datatable_rows(payload) == [
        {"ticker": "AAPL", "v": 1}, {"ticker": "MSFT", "v": 2}]
    assert sharadar.datatable_rows({}) == []          # empty / missing -> []


def test_next_cursor_reads_meta():
    assert sharadar.next_cursor({"meta": {"next_cursor_id": "abc"}}) == "abc"
    assert sharadar.next_cursor({"meta": {"next_cursor_id": None}}) is None
    assert sharadar.next_cursor({}) is None


def _page(rows, cursor):
    return {"datatable": {"columns": [{"name": "ticker"}, {"name": "v"}], "data": rows},
            "meta": {"next_cursor_id": cursor}}


def test_fetch_table_paginates_and_threads_cursor(monkeypatch):
    pages = [_page([["A", 1], ["B", 2]], "c1"), _page([["C", 3]], None)]
    seen, calls = [], {"i": 0}

    def fake_get(url, params, secret):
        seen.append(dict(params))
        pg = pages[calls["i"]]
        calls["i"] += 1
        return pg

    monkeypatch.setattr(sharadar, "_get", fake_get)
    rows = sharadar.fetch_table("SF1", "k", params={"ticker": "AAPL"})
    assert [r["ticker"] for r in rows] == ["A", "B", "C"]     # all pages accumulated
    assert "qopts.cursor_id" not in seen[0]                   # first page: no cursor
    assert seen[1]["qopts.cursor_id"] == "c1"                 # second page: cursor threaded
    assert seen[0]["api_key"] == "k" and seen[0]["ticker"] == "AAPL"


def test_fetch_table_guards(monkeypatch):
    # unknown table -> [] without any network call
    monkeypatch.setattr(sharadar, "_get",
                        lambda *a, **k: pytest.fail("should not fetch"))
    assert sharadar.fetch_table("NOT_A_TABLE", "k") == []
    assert sharadar.fetch_table("SF1", "") == []             # no key -> []


def test_fetch_table_fails_soft_on_none(monkeypatch):
    monkeypatch.setattr(sharadar, "_get", lambda *a, **k: None)
    assert sharadar.fetch_table("TICKERS", "k") == []


def test_scrub_removes_secret():
    assert sharadar._scrub("GET url?api_key=SEKRIT&x=1", "SEKRIT") == "GET url?api_key=***&x=1"
    assert sharadar._scrub("no secret here", None) == "no secret here"


@pytest.mark.skipif(not os.environ.get("RUN_LIVE_SHARADAR"),
                    reason="live Sharadar call — set RUN_LIVE_SHARADAR=1 (needs the key)")
def test_live_tickers_smoke():
    from app.config import load_config
    cfg = load_config()
    rows = sharadar.fetch_table("TICKERS", cfg.nasdaq_data_link_api_key,
                                params={"ticker": "AAPL"})
    assert rows and any(r.get("ticker") == "AAPL" for r in rows)
    assert all("permaticker" in r for r in rows)
