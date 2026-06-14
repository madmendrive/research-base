"""Resolve regulator codes for a ticker so it becomes downloadable.

- US  -> sec_cik   (SEC company_tickers.json, matched by ticker + name agreement)
- KR  -> dart_code (DART corpCode.xml, matched by 6-digit stock code)
- TW  -> twse_code (derived from the "XXXX TT" ticker number)
- JP  -> nothing stored; EDINET resolves by the securities code in the ticker

Network maps are cached per process (lru_cache). All HTTPS goes through the
truststore the entry points inject (Norton MITM on this machine).
"""
import io
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from functools import lru_cache

import requests

_SEC_URL = "https://www.sec.gov/files/company_tickers.json"
_DART_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
_UA = "research-pipeline (ernest.limwj@gmail.com)"  # SEC requires a contact UA

MARKET_CODE_FIELD = {"US": "sec_cik", "KR": "dart_code", "TW": "twse_code"}

_SUFFIXES = {"inc", "corp", "corporation", "co", "company", "ltd", "limited",
             "holdings", "holding", "technologies", "technology", "tech", "group",
             "the", "plc", "sa", "nv", "ag", "llc", "lp", "semiconductor", "semiconductors"}


def _toks(s):
    return {w for w in re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split()
            if w and w not in _SUFFIXES}


def _names_agree(a, b):
    ta, tb = _toks(a), _toks(b)
    return bool(ta and tb and (ta & tb))


@lru_cache(maxsize=1)
def _sec_map():
    """{TICKER -> (cik10, title)} from SEC."""
    r = requests.get(_SEC_URL, headers={"User-Agent": _UA}, timeout=60)
    r.raise_for_status()
    return {str(v["ticker"]).upper(): (str(v["cik_str"]).zfill(10), str(v.get("title", "")))
            for v in r.json().values()}


@lru_cache(maxsize=1)
def _dart_map():
    """{6-digit stock_code -> 8-digit corp_code} from DART. Empty if no API key."""
    key = os.getenv("DART_API_KEY")
    if not key:
        return {}
    r = requests.get(_DART_URL, params={"crtfc_key": key}, timeout=120)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        xml = z.read(z.namelist()[0])
    out = {}
    for li in ET.fromstring(xml).findall("list"):
        sc = (li.findtext("stock_code") or "").strip()
        cc = (li.findtext("corp_code") or "").strip()
        if sc and sc != " " and cc:
            out[sc.zfill(6)] = cc
    return out


def resolve_code(ticker, name="", market="") -> dict:
    """Return {field: value} of the regulator code for `ticker`, or {} if none
    applies / can't be resolved. Network errors propagate to the caller (which
    should treat resolution as best-effort)."""
    market = (market or "").upper()
    num = ticker.split()[0] if ticker else ""
    if market == "US":
        hit = _sec_map().get(ticker.upper())
        if hit and (not name or _names_agree(name, hit[1])):
            return {"sec_cik": hit[0]}
    elif market == "TW":
        if num.isdigit():
            return {"twse_code": num}
    elif market == "KR":
        if num.isdigit():
            cc = _dart_map().get(num.zfill(6))
            if cc:
                return {"dart_code": cc}
    # JP and everything else: no stored regulator code needed.
    return {}
