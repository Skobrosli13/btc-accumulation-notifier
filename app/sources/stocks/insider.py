"""SEC EDGAR Form-4 insider adapter (keyless; User-Agent + ≤10 req/s required).

Live path for the insider-cluster context signal: the owner-only Form-4 Atom feed
per CIK gives recent filings within minutes; each filing's XML
(``ownershipDocument``) carries the transaction code (``P`` = open-market buy),
shares, price and the owner's officer/director relationship. We keep only what a
cluster read needs and stay bounded (recent filings only). Fail-soft: any failure
-> ``[]`` so the layer just goes dark.

Note: cluster-buying edge is a small/mid-cap phenomenon — in S&P-500 mega-caps this
is a weak confirmation layer, not a driver (hence context-only, never a setup).
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from .._http import get_json, get_text

log = logging.getLogger(__name__)

BROWSE = "https://www.sec.gov/cgi-bin/browse-edgar"
ARCHIVE_INDEX = "https://www.sec.gov/Archives/edgar/data/{cikdir}/{acc}/index.json"
ARCHIVE_FILE = "https://www.sec.gov/Archives/edgar/data/{cikdir}/{acc}/{name}"
_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
_HREF_RE = re.compile(r"/data/(\d+)/(\d+)/([\d-]+)-index")


def _date_ms(s: str) -> int | None:
    try:
        d = datetime.strptime(s[:10], "%Y-%m-%d")
        return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _recent_filings(cik: str, user_agent: str, count: int) -> list[dict]:
    """[{cikdir, acc_nodash, filed_ts, index_href}] from the owner-only Form-4 feed."""
    txt = get_text(BROWSE, params={"action": "getcompany", "CIK": cik, "type": "4",
                                   "owner": "only", "count": count, "output": "atom"},
                   headers={"User-Agent": user_agent})
    if not txt or "<entry" not in txt:
        return []
    try:
        root = ET.fromstring(txt)
    except ET.ParseError:
        return []
    out = []
    for entry in root.findall("a:entry", _ATOM_NS):
        link = entry.find("a:link", _ATOM_NS)
        href = link.get("href") if link is not None else ""
        m = _HREF_RE.search(href or "")
        if not m:
            continue
        filed = entry.find("a:updated", _ATOM_NS)
        out.append({"cikdir": m.group(1), "acc_nodash": m.group(2),
                    "acc_dashed": m.group(3),
                    "filed_ts": _date_ms(filed.text) if filed is not None else None})
    return out


def _form4_xml_name(cikdir: str, acc: str, user_agent: str) -> str | None:
    """Find the primary Form-4 XML filename in a filing directory."""
    data = get_json(ARCHIVE_INDEX.format(cikdir=cikdir, acc=acc),
                    headers={"User-Agent": user_agent})
    items = ((data or {}).get("directory") or {}).get("item") or []
    xmls = [it.get("name", "") for it in items if it.get("name", "").lower().endswith(".xml")]
    # Prefer a name that looks like a form-4 doc; skip rendered 'R*.xml'.
    for name in xmls:
        low = name.lower()
        if low.startswith("r") and low[1:2].isdigit():
            continue
        if "form4" in low or "wf-form4" in low or "wk-form4" in low or "ownership" in low:
            return name
    for name in xmls:
        if not (name.lower().startswith("r") and name[1:2].isdigit()):
            return name
    return None


def _txt(node, tag) -> str | None:
    """Text of ``tag/value`` (Form-4 wraps most leaves in <value>) or ``tag``."""
    el = node.find(f"{tag}/value")
    if el is not None and el.text is not None:
        return el.text.strip()
    el = node.find(tag)
    return el.text.strip() if (el is not None and el.text is not None) else None


def _parse_form4(xml_text: str) -> dict | None:
    """Parse an ownershipDocument -> {ticker, owner, is_officer, is_director, txns[]}."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    if root.tag != "ownershipDocument":
        return None
    ticker = _txt(root, "issuer/issuerTradingSymbol")
    owner = root.find("reportingOwner")
    name = _txt(owner, "reportingOwnerId/rptOwnerName") if owner is not None else None
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    def _flag(tag):
        v = (_txt(rel, tag) or "").strip().lower() if rel is not None else ""
        return 1 if v in ("1", "true") else 0
    is_officer, is_director = _flag("isOfficer"), _flag("isDirector")
    txns = []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _txt(t, "transactionCoding/transactionCode")
        shares = _txt(t, "transactionAmounts/transactionShares")
        price = _txt(t, "transactionAmounts/transactionPricePerShare")
        tdate = _txt(t, "transactionDate")
        try:
            sh = float(shares) if shares else None
            pr = float(price) if price else None
        except ValueError:
            sh = pr = None
        txns.append({"code": code, "shares": sh, "price": pr,
                     "txn_ts": _date_ms(tdate or ""),
                     "value": (sh * pr) if (sh and pr) else None})
    return {"ticker": (ticker or "").upper(), "owner": name,
            "is_officer": is_officer, "is_director": is_director, "txns": txns}


def insider_transactions(cik: str, ticker: str, user_agent: str, since_ts: int,
                         max_filings: int = 12) -> list[dict]:
    """Recent Form-4 transactions for a CIK filed since ``since_ts`` -> stock_insider rows.
    Bounded to ``max_filings`` recent filings; [] on any failure / no CIK."""
    if not cik:
        return []
    rows: list[dict] = []
    for f in _recent_filings(cik, user_agent, max_filings):
        if f.get("filed_ts") is not None and f["filed_ts"] < since_ts:
            continue
        name = _form4_xml_name(f["cikdir"], f["acc_nodash"], user_agent)
        if not name:
            continue
        xml_text = get_text(ARCHIVE_FILE.format(cikdir=f["cikdir"], acc=f["acc_nodash"], name=name),
                            headers={"User-Agent": user_agent})
        parsed = _parse_form4(xml_text or "")
        if not parsed:
            continue
        acc_dashed = f["acc_dashed"]
        for i, t in enumerate(parsed["txns"]):
            if not t.get("code"):
                continue
            rows.append({
                "accession": f"{acc_dashed}-{i}", "ticker": ticker, "cik": cik,
                "insider": parsed.get("owner"), "is_officer": parsed["is_officer"],
                "is_director": parsed["is_director"], "txn_code": t["code"],
                "txn_ts": t.get("txn_ts") or f.get("filed_ts"), "shares": t.get("shares"),
                "price": t.get("price"), "value": t.get("value"), "filed_ts": f.get("filed_ts"),
            })
    return rows
