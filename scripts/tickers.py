"""Ticker canonicalisation helpers.

The research extractor sees the same listed company in several dialects:
Bloomberg-style symbols such as ``3037 TT``, Yahoo-style symbols such as
``3037.TW``, and occasionally bare local codes. This module keeps those aliases
mapped to one canonical symbol before they enter durable memory tables.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPANIES_PATH = PROJECT_ROOT / "config" / "companies.json"

MARKET_SUFFIXES = {
    "KS",
    "KQ",
    "TT",
    "TW",
    "TWO",
    "JT",
    "JP",
    "T",
    "HK",
    "SZ",
    "SS",
    "SH",
    "SP",
    "SI",
    "AV",
    "IM",
    "LN",
}

DEFAULT_SUFFIX_MAP = {
    "KS": "KS",
    "KQ": "KQ",
    "TT": "TT",
    "TW": "TT",
    # Bloomberg TT covers both TWSE and TPEx; Yahoo-style .TWO (TPEx) folds in
    # so e.g. 6223 TWO and 6223 TT don't become separate subjects.
    "TWO": "TT",
    "T": "JT",
    "JT": "JT",
    "JP": "JT",
    "HK": "HK",
    "SZ": "SZ",
    "SS": "SS",
    "SH": "SS",
    "SP": "SP",
    "SI": "SP",
    "AV": "AV",
    "IM": "IM",
    "LN": "LN",
}


def _load_companies() -> dict:
    try:
        return json.loads(COMPANIES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _alias_key(value: str) -> str:
    value = (value or "").strip().upper()
    value = value.replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", value)


def _split_symbol(value: str) -> tuple[str, str] | None:
    value = _alias_key(value)
    match = re.match(r"^([A-Z0-9]{1,8})[ .]([A-Z]{1,4})$", value)
    if not match:
        return None
    code, suffix = match.groups()
    if suffix not in MARKET_SUFFIXES:
        return None
    return code, suffix


def _add_alias(alias_map: dict[str, str], alias: str, canonical: str) -> None:
    key = _alias_key(alias)
    if key:
        alias_map.setdefault(key, canonical)


_CORP_SUFFIXES = re.compile(
    r"\b(inc|corp|corporation|co|company|ltd|limited|holdings?|plc|technologies|"
    r"technology|group|ag|sa|nv|llc|lp|the)\b",
    re.I,
)


def _normalize_name(name: str) -> str:
    """Strip punctuation + corporate suffixes so 'Chroma ATE Inc' -> 'chroma ate'."""
    n = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    n = _CORP_SUFFIXES.sub(" ", n)
    return re.sub(r"\s+", " ", n).strip()


@lru_cache(maxsize=1)
def alias_map() -> dict[str, str]:
    companies = _load_companies()
    aliases: dict[str, str] = {}
    code_to_canonicals: dict[str, set[str]] = defaultdict(set)

    for canonical in companies:
        _add_alias(aliases, canonical, canonical)
        split = _split_symbol(canonical)
        if not split:
            continue
        code, suffix = split
        canonical_suffix = DEFAULT_SUFFIX_MAP.get(suffix, suffix)
        code_to_canonicals[code].add(canonical)
        for alias_suffix in {suffix, canonical_suffix}:
            _add_alias(aliases, f"{code} {alias_suffix}", canonical)
            _add_alias(aliases, f"{code}.{alias_suffix}", canonical)
        if canonical_suffix == "TT":
            _add_alias(aliases, f"{code}.TW", canonical)
            _add_alias(aliases, f"{code} TW", canonical)
        elif canonical_suffix == "JT":
            _add_alias(aliases, f"{code}.T", canonical)
            _add_alias(aliases, f"{code} JP", canonical)
            _add_alias(aliases, f"{code}.JP", canonical)
        elif canonical_suffix == "KS":
            _add_alias(aliases, f"{code}.KS", canonical)
        elif canonical_suffix == "HK":
            _add_alias(aliases, f"{code}.HK", canonical)

    for code, canonicals in code_to_canonicals.items():
        if len(canonicals) == 1:
            _add_alias(aliases, code, next(iter(canonicals)))

    # Company-name aliases: let "Chroma ATE" resolve to 2360 TT. Added only when a
    # normalized name maps to a single ticker (skip ambiguous collisions); ticker-
    # form aliases above keep priority via _add_alias's setdefault.
    name_to_canon: dict[str, set[str]] = defaultdict(set)
    raw_name: dict[str, str] = {}
    for canonical, meta in companies.items():
        norm = _normalize_name((meta or {}).get("name") or "")
        if norm:
            name_to_canon[norm].add(canonical)
            raw_name.setdefault(canonical, ((meta or {}).get("name") or "").strip())
    for norm, canonicals in name_to_canon.items():
        if len(canonicals) == 1:
            canonical = next(iter(canonicals))
            _add_alias(aliases, norm, canonical)               # suffix-stripped form
            _add_alias(aliases, raw_name[canonical], canonical)  # full company name

    return aliases


def canonicalize_ticker(value: str | None) -> str | None:
    if value is None:
        return None
    original = str(value).strip()
    if not original:
        return original

    aliases = alias_map()
    direct = aliases.get(_alias_key(original))
    if direct:
        return direct

    split = _split_symbol(original)
    if split:
        code, suffix = split
        canonical_suffix = DEFAULT_SUFFIX_MAP.get(suffix, suffix)
        candidates = [
            f"{code} {canonical_suffix}",
            f"{code}.{canonical_suffix}",
            f"{code} {suffix}",
            f"{code}.{suffix}",
        ]
        for candidate in candidates:
            mapped = aliases.get(_alias_key(candidate))
            if mapped:
                return mapped
        return f"{code} {canonical_suffix}"

    code_key = _alias_key(original)
    if re.match(r"^[0-9]{3,6}[A-Z]?$", code_key):
        mapped = aliases.get(code_key)
        if mapped:
            return mapped

    return original


def canonicalize_subject(subject_type: str | None, subject: str | None) -> str | None:
    if (subject_type or "").lower() in {"ticker", "single_name"}:
        return canonicalize_ticker(subject)
    return subject


def canonicalize_ticker_list(values) -> list[str]:
    seen = set()
    out = []
    for value in values or []:
        canonical = canonicalize_ticker(value)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        out.append(canonical)
    return out


def canonicalize_ticker_materiality(values: dict | None) -> dict:
    out = {}
    for ticker, materiality in (values or {}).items():
        canonical = canonicalize_ticker(ticker)
        if canonical:
            out[canonical] = materiality
    return out


# --- Ticker format validation (guards write paths against name/placeholder keys) ---
_VALID_TICKER_FORMATS = (
    re.compile(r"^[A-Z]{1,6}$"),                            # US bare symbol
    re.compile(r"^\d{3,6}[A-Z]? (TT|JT|KS|HK|SP|CH)$"),     # Asia numeric (+optional letter) + suffix
    re.compile(r"^[A-Z0-9]{1,6} (SP|AV|LN|GR|FP|IM|TT)$"),  # lettered code + exchange suffix
)


def is_valid_ticker(ticker) -> bool:
    """True if `ticker` is a well-formed market ticker (not a company name or placeholder)."""
    t = (ticker or "").strip()
    if not t or "placeholder" in t.lower():
        return False
    return any(rx.match(t) for rx in _VALID_TICKER_FORMATS)
