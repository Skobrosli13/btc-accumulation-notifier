"""Universe loader + SEC ticker→CIK resolver.

Reads the committed ``stock_universe.json`` (ticker + sector) and backfills each
name + zero-padded 10-digit CIK from SEC's free ``company_tickers.json`` map
(keyless, one small request). CIK is what the EDGAR Form-4 path keys on, so we
resolve it once and persist it into ``stock_universe``.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .._http import get_json

log = logging.getLogger(__name__)

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


def _resolve_path(path: str) -> Path | None:
    """Find the universe file whether ``path`` is absolute, cwd-relative, repo-root
    relative, or just a bare filename living next to the app package."""
    here = Path(__file__).resolve()
    repo_root = here.parents[3]          # .../btc-accumulation-notifier
    app_dir = here.parents[2]            # .../btc-accumulation-notifier/app
    for cand in (Path(path), repo_root / path, app_dir / Path(path).name):
        if cand.is_file():
            return cand
    return None


def read_universe_file(path: str) -> list[dict]:
    """Parse the committed universe JSON -> [{ticker, sector}, ...]. [] on failure."""
    p = _resolve_path(path)
    if p is None:
        log.warning("universe file %s not found (cwd/repo-root/app)", path)
        return []
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("universe file %s unreadable: %s", path, exc)
        return []
    rows = data.get("tickers") if isinstance(data, dict) else data
    out = []
    for r in rows or []:
        t = (r.get("ticker") or "").strip().upper()
        if t:
            out.append({"ticker": t, "sector": r.get("sector")})
    return out


def sec_ticker_map(user_agent: str) -> dict[str, dict]:
    """{TICKER: {"cik": "0000320193", "name": "Apple Inc."}} from SEC. {} on failure.

    SEC fair-access requires a descriptive User-Agent; without one it returns 403.
    """
    data = get_json(SEC_TICKERS_URL, headers={"User-Agent": user_agent,
                                              "Accept-Encoding": "gzip, deflate"})
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for _, row in data.items():
        try:
            tk = str(row["ticker"]).upper()
            out[tk] = {"cik": f"{int(row['cik_str']):010d}", "name": row.get("title")}
        except (KeyError, TypeError, ValueError):
            continue
    return out


def resolve_universe(path: str, user_agent: str) -> list[tuple[str, str | None, str | None, str | None]]:
    """Merge the file with the SEC map -> [(ticker, name, sector, cik|None), ...] ready
    for ``stock_store.upsert_universe``. Missing SEC entries just get a None cik/name
    (the ticker still trades and prices/earnings work; only the insider layer needs CIK)."""
    base = read_universe_file(path)
    smap = sec_ticker_map(user_agent)
    if not smap:
        log.warning("SEC ticker map empty — CIKs unresolved this sync (insider layer dark until resolved)")
    out = []
    for r in base:
        tk = r["ticker"]
        meta = smap.get(tk) or {}
        out.append((tk, meta.get("name"), r.get("sector"), meta.get("cik")))
    return out
