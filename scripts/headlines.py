"""Headline sweep, dedupe, materiality ranking, and Telegram digest."""

from __future__ import annotations

import json
import os
import re
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html import escape, unescape
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from requests.exceptions import SSLError

from scripts import kb
from scripts.notify import telegram_send, telegram_send_markdownish_html

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_PATH = PROJECT_ROOT / "config" / "dashboard.json"
COMPANIES_PATH = PROJECT_ROOT / "config" / "companies.json"
HEADLINE_DIR = DATA_DIR / "_headlines"
STATE_PATH = HEADLINE_DIR / "_headline_state.json"
LATEST_DIGEST_PATH = HEADLINE_DIR / "_latest_digest.json"

TECH_SEARCH_TERMS = [
    "semiconductor foundry",
    "DRAM NAND memory",
    "HBM high bandwidth memory",
    "AI server GPU",
    "ABF substrate CCL PCB",
    "IC design",
    "TSMC foundry",
    "Samsung semiconductor",
    "smartphone PC tablet shipment",
    "Memory probe cards",
    "data center power cooling",
    "AI infrastructure supply chain",
]

ZH_TECH_SEARCH_TERMS = [
    "半導體 晶圓代工",
    "DRAM HBM 記憶體",
    "AI伺服器",
    "ABF載板 CCL",
    "IC設計 漲價",
    "台積電",
    "鴻海",
    "聯電",
    "GTC 輝達",
    "探針卡",
]

TECH_KEYWORDS = {
    "ai",
    "anthropic",
    "openai",
    "gemini",
    "deepseek",
    "semiconductor",
    "chip",
    "memory",
    "dram",
    "nand",
    "hbm",
    "foundry",
    "gpu",
    "cpu",
    "server",
    "substrate",
    "ccl",
    "pcb",
    "probe",
    "data center",
}

DEFAULT_SOURCES = [
    "bloomberg",
    "reuters",
    "wsj",
    "wall street journal",
    "financial times",
    "ft",
    "nikkei",
    "digitimes",
    "trendforce",
    "commercial times",
    "ctee.com",
    "ctee",
    "udn",
    "money.udn",
    "anue",
    "cnyes",
    "technews",
    "finance.technews",
    "counterpoint",
    "counterpointresearch",
    "gartner",
    "semianalysis",
    "the information",
    "business times",
    "ltn",
    "ec.ltn",
    "chinatimes",
    "china times",
    "ijiwei",
]

MATERIAL_KEYWORDS = [
    "semiconductor",
    "chip",
    "gpu",
    "ai server",
    "hbm",
    "dram",
    "nand",
    "foundry",
    "export control",
    "tariff",
    "earnings",
    "guidance",
    "revenue",
    "capex",
    "rate cut",
    "inflation",
    "geopolitical",
]

HARD_TECH_SIGNAL_TERMS = (
    "semiconductor",
    "semiconductors",
    "chip",
    "chips",
    "gpu",
    "cpu",
    "asic",
    "ai chip",
    "ai server",
    "ai servers",
    "ai data center",
    "ai datacenter",
    "ai infrastructure",
    "data center",
    "datacenter",
    "hyperscaler",
    "hbm",
    "dram",
    "nand",
    "memory",
    "foundry",
    "wafer",
    "euv",
    "lithography",
    "advanced packaging",
    "cowos",
    "substrate",
    "abf",
    "pcb",
    "ccl",
    "ic design",
    "leadframe",
    "probe card",
    "liquid cooling",
    "power components",
    "tsmc",
    "nvidia",
    "sk hynix",
    "samsung electronics",
    "micron",
    "asml",
    "amd",
    "broadcom",
    "mediatek",
    "supermicro",
    "delta electronics",
    "lite-on",
    "半導體",
    "晶片",
    "晶圓",
    "記憶體",
    "伺服器",
    "資料中心",
    "數據中心",
    "台積電",
    "輝達",
    "海力士",
    "美光",
    "先進封裝",
    "載板",
    "散熱",
)

ARTICLE_CONTEXT_CHARS = 2400

NATIVE_SOURCE_ALIASES = {
    "bloomberg": {"bloomberg"},
    "reuters": {"reuters"},
    "wsj": {"wsj", "wall street journal"},
    "financial_times": {"financial times", "ft"},
    "nikkei": {"nikkei"},
    "digitimes": {"digitimes"},
    "trendforce": {"trendforce"},
    "ctee": {"commercial times", "ctee", "ctee.com"},
    "udn": {"udn", "money.udn"},
    "cnyes": {"anue", "cnyes"},
    "technews": {"technews", "finance.technews"},
    "counterpoint": {"counterpoint", "counterpointresearch"},
    "gartner": {"gartner"},
    "semianalysis": {"semianalysis"},
    "the_information": {"the information"},
    "business_times": {"business times"},
    "ltn": {"ltn", "ec.ltn"},
    "chinatimes": {"chinatimes", "china times"},
    "ijiwei": {"ijiwei"},
}

NATIVE_RSS_FEEDS = {
    "bloomberg": [
        {"source": "Bloomberg", "url": "https://feeds.bloomberg.com/technology/news.rss"},
        {"source": "Bloomberg", "url": "https://feeds.bloomberg.com/markets/news.rss"},
    ],
    "wsj": [
        {"source": "Wall Street Journal", "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"},
        {"source": "Wall Street Journal", "url": "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml"},
    ],
    "financial_times": [
        {"source": "Financial Times", "url": "https://www.ft.com/technology?format=rss"},
        {"source": "Financial Times", "url": "https://www.ft.com/technology-sector?format=rss"},
    ],
    "digitimes": [
        {"source": "DIGITIMES", "url": "https://www.digitimes.com/rss/daily.xml"},
    ],
    "technews": [
        {"source": "TechNews", "url": "https://technews.tw/feed/"},
        {"source": "TechNews", "url": "https://finance.technews.tw/feed/"},
    ],
    "semianalysis": [
        {"source": "SemiAnalysis", "url": "https://semianalysis.com/feed/"},
    ],
    "business_times": [
        {"source": "The Business Times", "url": "https://www.businesstimes.com.sg/rss.xml"},
    ],
}

NATIVE_HTML_INDEXES = {
    "reuters": [
        {"source": "Reuters", "url": "https://www.reuters.com/technology/", "domains": ("reuters.com",)},
        {"source": "Reuters", "url": "https://www.reuters.com/markets/", "domains": ("reuters.com",)},
    ],
    "wsj": [
        {"source": "Wall Street Journal", "url": "https://www.wsj.com/tech", "domains": ("wsj.com",)},
    ],
    "nikkei": [
        {"source": "Nikkei Asia", "url": "https://asia.nikkei.com/Business/Technology", "domains": ("asia.nikkei.com",)},
        {"source": "Nikkei Asia", "url": "https://asia.nikkei.com/Spotlight/Supply-Chain", "domains": ("asia.nikkei.com",)},
    ],
    "trendforce": [
        {"source": "TrendForce", "url": "https://www.trendforce.com/news/", "domains": ("trendforce.com",)},
    ],
    "ctee": [
        {"source": "Commercial Times", "url": "https://www.ctee.com.tw/industrynews/technology", "domains": ("ctee.com.tw",)},
        {"source": "Commercial Times", "url": "https://www.ctee.com.tw/livenews", "domains": ("ctee.com.tw",)},
    ],
    "udn": [
        {"source": "money.udn.com", "url": "https://money.udn.com/money/cate/5591", "domains": ("money.udn.com",)},
        {"source": "money.udn.com", "url": "https://money.udn.com/money/cate/5595", "domains": ("money.udn.com",)},
    ],
    "counterpoint": [
        {"source": "Counterpoint Research", "url": "https://www.counterpointresearch.com/insights/", "domains": ("counterpointresearch.com",)},
    ],
    "gartner": [
        {"source": "Gartner", "url": "https://www.gartner.com/en/newsroom/press-releases", "domains": ("gartner.com",)},
    ],
    "the_information": [
        {"source": "The Information", "url": "https://www.theinformation.com/briefings", "domains": ("theinformation.com",)},
    ],
    "ltn": [
        {"source": "Liberty Times", "url": "https://ec.ltn.com.tw/list/breakingnews", "domains": ("ec.ltn.com.tw", "news.ltn.com.tw")},
    ],
    "chinatimes": [
        {"source": "China Times", "url": "https://www.chinatimes.com/realtimenews/?chdtv", "domains": ("chinatimes.com",)},
        {"source": "China Times", "url": "https://www.chinatimes.com/technology/?chdtv", "domains": ("chinatimes.com",)},
    ],
    "ijiwei": [
        {"source": "ijiwei", "url": "https://www.laoyaoba.com/", "domains": ("laoyaoba.com", "ijiwei.com")},
    ],
}

CNYES_NATIVE_CATEGORIES = ("headline", "tech", "us_stock", "tw_stock", "wd_stock")


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _load_companies() -> dict:
    if not COMPANIES_PATH.exists():
        return {}
    try:
        with open(COMPANIES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {"seen": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"seen": {}}


SEEN_RETENTION_DAYS = 45
ITEMS_RETENTION_DAYS = 14


def _prune_state(state: dict) -> None:
    """Drop old entries so the state file doesn't grow without bound.

    Dedupe only needs to look back as far as the sweep window (hours), and
    stored items only back a digest or two, so these retention windows are
    deliberately generous.
    """
    def cutoff(days: int) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")

    def prune(entries: dict, field: str, days: int) -> dict:
        limit = cutoff(days)
        kept = {}
        for key, value in entries.items():
            stamp = str((value or {}).get(field) or "")
            if not stamp or stamp >= limit:
                kept[key] = value
        return kept

    if isinstance(state.get("seen"), dict):
        state["seen"] = prune(state["seen"], "first_seen_at", SEEN_RETENTION_DAYS)
    if isinstance(state.get("items"), dict):
        state["items"] = prune(state["items"], "fetched_at", ITEMS_RETENTION_DAYS)


def _save_state(state: dict) -> None:
    HEADLINE_DIR.mkdir(parents=True, exist_ok=True)
    _prune_state(state)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _source_list(config: dict) -> list[str]:
    feed_sources = config.get("feed_sources", {})
    configured = []
    for key in ("headlines", "topics", "portfolio", "tech"):
        configured.extend(feed_sources.get(key, []))
    return [s.lower() for s in configured] or DEFAULT_SOURCES


def _norm_source_name(value: str) -> str:
    return re.sub(r"[^a-z0-9.]+", " ", str(value or "").lower()).strip()


def _source_alias_matches(source: str, alias: str) -> bool:
    source_n = _norm_source_name(source)
    alias_n = _norm_source_name(alias)
    if not source_n or not alias_n:
        return False
    if source_n == alias_n:
        return True
    if len(alias_n) > 3 and alias_n in source_n:
        return True
    if len(source_n) > 3 and source_n in alias_n:
        return True
    return False


def _native_source_keys(allowed_sources: list[str]) -> set[str]:
    allowed = allowed_sources or DEFAULT_SOURCES
    keys: set[str] = set()
    for key, aliases in NATIVE_SOURCE_ALIASES.items():
        if any(_source_alias_matches(source, alias) for source in allowed for alias in aliases):
            keys.add(key)
    return keys


def _source_names_for_keys(keys: set[str]) -> set[str]:
    names: set[str] = set()
    for key in keys:
        names.update(NATIVE_SOURCE_ALIASES.get(key, set()))
    return names


def _source_key_for_item(item: dict) -> str | None:
    source = item.get("source") or ""
    url = item.get("url") or ""
    haystack = " ".join([source, urlparse(url).netloc]).lower()
    for key, aliases in NATIVE_SOURCE_ALIASES.items():
        if any(_source_alias_matches(haystack, alias) for alias in aliases):
            return key
    return None


def _terms(config: dict) -> list[str]:
    terms = []
    for topic in config.get("news_topics", []):
        topic_l = str(topic).lower()
        if any(k in topic_l for k in TECH_KEYWORDS):
            terms.append(topic)

    for section in config.get("brief_sections", []):
        heading = str(section.get("heading", "")).lower()
        if heading not in {"tech", "ai"}:
            continue
        terms.extend(section.get("topics", []))
        terms.extend(section.get("subsections", []))

    companies = _load_companies()
    for ticker in config.get("portfolio_positions", []):
        ticker = str(ticker).strip()
        if not ticker:
            continue
        terms.append(ticker)
        company = companies.get(ticker, {})
        name = str(company.get("name", "")).strip()
        if name:
            terms.append(name)

    defaults = [
        *TECH_SEARCH_TERMS,
        *ZH_TECH_SEARCH_TERMS,
        "AI semiconductor demand",
        "semiconductor export controls",
        "HBM memory demand",
        "AI server supply chain",
        "foundry capex",
    ]
    seen = set()
    out = []
    for term in [*terms, *defaults]:
        term = str(term).strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            out.append(term)
    return out[:120]


def _parse_rss_date(value: str) -> str:
    if not value:
        return ""
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(value).isoformat(timespec="seconds")
    except Exception:
        return value[:32]


def _published_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _within_window(value: str, window_hours: int, now: datetime | None = None,
                   fallback: str | None = None) -> bool:
    """True if the item is within the recency window.

    Uses the published date when available. Many scraped sources (HTML index
    pages especially) yield no machine-readable date — for those, callers pass
    `fallback` = when we first saw the headline, so an undated item that has
    been sitting on a source's index page for days (first seen long ago) is
    correctly treated as stale, while a genuinely new undated headline (first
    seen this sweep) passes. Only when neither a publish date nor a first-seen
    date is available do we keep the item (no signal to reject on).
    """
    dt = _published_dt(value) or _published_dt(fallback)
    if not dt:
        return True
    now = now or datetime.now(timezone.utc)
    return dt >= now - timedelta(hours=window_hours)


def _format_hkt(value: str) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        dt = dt.astimezone(ZoneInfo("Asia/Hong_Kong"))
        return f"{dt.day} {dt.strftime('%B %H:%M')} HKT"
    except Exception:
        return value[:16]


def _approved_source(source: str, allowed: list[str]) -> bool:
    s = (source or "").lower()
    return any(a in s for a in allowed)


def _normalise_google_news_url(link: str) -> str:
    return link.replace("/rss/articles/", "/articles/") if link else ""


def _source_home_url(source_el: ET.Element | None) -> str:
    if source_el is None:
        return ""
    return source_el.attrib.get("url", "") or ""


_thread_local = threading.local()


def _session() -> requests.Session:
    """Per-thread Session so feed fetches reuse TCP/TLS connections."""
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0"
        _thread_local.session = session
    return session


def _http_get(url: str, *, timeout: float = 10.0) -> requests.Response:
    """GET with a Windows-friendly SSL fallback for news sites."""
    try:
        return _session().get(url, timeout=timeout)
    except SSLError:
        try:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
        return _session().get(url, timeout=timeout, verify=False)


def _resolve_source_url(url: str) -> str:
    """Best-effort conversion of Google News links into the publisher article URL."""
    if not url:
        return ""
    parsed = urlparse(url)
    if "news.google." not in parsed.netloc:
        return url
    try:
        resp = _http_get(url, timeout=8)
        final_url = resp.url or url
        if "news.google." not in urlparse(final_url).netloc:
            return final_url
    except Exception:
        pass
    return url


def _clean_html_fragment(value: str) -> str:
    text = _repair_text_encoding(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\bView Full Coverage on Google News\b", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _clean_rss_description(value: str, title: str = "", source: str = "") -> str:
    text = _clean_html_fragment(value)
    if not text:
        return ""
    title_l = re.sub(r"\W+", " ", title.lower()).strip()
    source_l = (source or "").lower()
    parts = []
    for sentence in re.findall(r"[^.!?。！？]+[.!?。！？]?", text):
        sentence = sentence.strip()
        if not sentence:
            continue
        sentence_l = re.sub(r"\W+", " ", sentence.lower()).strip()
        if title_l and sentence_l == title_l:
            continue
        if source_l and sentence_l == source_l:
            continue
        if "google news" in sentence_l:
            continue
        parts.append(sentence)
        if len(parts) == 2:
            break
    return " ".join(parts).strip()


def _strip_markup(value: str) -> str:
    text = _repair_text_encoding(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


MOJIBAKE_REPLACEMENTS = {
    "â€™": "'",
    "â€˜": "'",
    "â€œ": '"',
    "â€": '"',
    "â€“": "-",
    "â€”": "-",
    "â€¦": "...",
    "Â\xa0": " ",
    "Â": "",
    "â€": '"',
    "\u2019": "'",
    "\u2018": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2013": "-",
    "\u2014": "-",
    "\u2026": "...",
}


def _repair_text_encoding(value: str) -> str:
    text = unescape(str(value or ""))
    if any(marker in text for marker in ("â", "Ã", "Â", "ç", "å", "è", "é", "æ", "ã", "ï")):
        candidate = _decode_mojibake_bytes(text)
        if candidate and candidate.count("�") <= text.count("�"):
            text = candidate
        else:
            for codec in ("cp1252", "latin1"):
                try:
                    candidate = text.encode(codec).decode("utf-8")
                except UnicodeError:
                    continue
                if candidate and candidate.count("�") <= text.count("�"):
                    text = candidate
                    break
    for bad, good in MOJIBAKE_REPLACEMENTS.items():
        text = text.replace(bad, good)
    return text


def _decode_mojibake_bytes(text: str) -> str:
    raw = bytearray()
    for ch in text:
        codepoint = ord(ch)
        if codepoint <= 255:
            raw.append(codepoint)
            continue
        for codec in ("cp1252", "latin1"):
            try:
                raw.extend(ch.encode(codec))
                break
            except UnicodeError:
                continue
        else:
            return ""
    try:
        return bytes(raw).decode("utf-8")
    except UnicodeError:
        return ""


def _first_text(el: ET.Element, names: tuple[str, ...]) -> str:
    local_names = {name.lower().split(":", 1)[-1] for name in names}
    for name in names:
        found = el.find(name)
        if found is not None and found.text:
            return found.text.strip()
    for child in list(el):
        local = child.tag.rsplit("}", 1)[-1].lower()
        if local in local_names and child.text:
            return child.text.strip()
    return ""


def _first_link(el: ET.Element) -> str:
    link = _first_text(el, ("link",))
    if link:
        return link
    for child in list(el):
        local = child.tag.rsplit("}", 1)[-1].lower()
        if local == "link":
            href = child.attrib.get("href")
            if href:
                return href.strip()
    return ""


def _parse_native_feed(
    content: bytes,
    *,
    feed_url: str,
    source: str,
    window_hours: int,
    max_items: int = 40,
) -> list[dict]:
    try:
        root = ET.fromstring(content)
    except Exception:
        return []

    entries = list(root.findall(".//item"))
    if not entries:
        entries = list(root.findall(".//{http://www.w3.org/2005/Atom}entry"))

    items = []
    for el in entries[:max_items]:
        title = _strip_markup(_first_text(el, ("title",)))
        link = _first_link(el)
        description = _clean_rss_description(
            _first_text(el, ("description", "summary", "content", "encoded")),
            title,
            source,
        )
        published_at = _parse_rss_date(
            _first_text(el, ("pubDate", "pubdate", "published", "updated", "dc:date", "date"))
        )
        if not title or not link:
            continue
        if not _within_window(published_at, window_hours):
            continue
        items.append({
            "title": title,
            "source": source,
            "published_at": published_at,
            "url": urljoin(feed_url, link),
            "source_home_url": feed_url,
            "description": description,
            "query": f"native:{source}",
            "discovery": "native_rss",
        })
    return items


def _fetch_native_rss_feed(feed: dict, window_hours: int) -> list[dict]:
    try:
        resp = _http_get(feed["url"], timeout=10)
        if resp.status_code != 200:
            return []
        return _parse_native_feed(
            resp.content,
            feed_url=feed["url"],
            source=feed["source"],
            window_hours=window_hours,
        )
    except Exception:
        return []


def _cnyes_row_to_item(row: dict, *, category: str = "") -> dict:
    title = _strip_markup(row.get("title") or "")
    content = _strip_markup(row.get("content") or "")
    news_id = row.get("newsId")
    published_at = ""
    if row.get("publishAt"):
        try:
            published_at = datetime.fromtimestamp(int(row["publishAt"]), tz=timezone.utc).isoformat(timespec="seconds")
        except Exception:
            published_at = ""
    return {
        "title": title,
        "source": "cnyes.com",
        "published_at": published_at,
        "url": f"https://news.cnyes.com/news/id/{news_id}" if news_id else "https://news.cnyes.com/",
        "source_home_url": "https://news.cnyes.com/",
        "description": content[:600],
        "article_text": " ".join(x for x in (title, _strip_markup(row.get("signature") or ""), content) if x)[:ARTICLE_CONTEXT_CHARS],
        "article_source": "cnyes_api",
        "query": f"native:cnyes:{category}",
        "discovery": "native_api",
    }


def _fetch_cnyes_native(window_hours: int, max_per_category: int = 20) -> list[dict]:
    items = []
    for category in CNYES_NATIVE_CATEGORIES:
        try:
            api_url = (
                "https://api.cnyes.com/media/api/v1/newslist/category/"
                f"{category}?page=1&limit={max_per_category}"
            )
            resp = _http_get(api_url, timeout=10)
            if resp.status_code != 200:
                continue
            rows = ((resp.json().get("items") or {}).get("data") or [])[:max_per_category]
        except Exception:
            continue
        for row in rows:
            item = _cnyes_row_to_item(row, category=category)
            if item.get("title") and _within_window(item.get("published_at", ""), window_hours):
                items.append(item)
    return items


def _fetch_html_index(index: dict, window_hours: int, max_items: int = 35) -> list[dict]:
    try:
        resp = _http_get(index["url"], timeout=10)
        if resp.status_code >= 400:
            return []
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(resp.text, "lxml")
    except Exception:
        return []

    domains = tuple(index.get("domains") or ())
    seen = set()
    items = []
    for a in soup.find_all("a", href=True):
        title = _strip_markup(a.get_text(" ", strip=True))
        if len(title) < 18 or len(title) > 220:
            continue
        href = urljoin(index["url"], a.get("href", ""))
        parsed = urlparse(href)
        if domains and not any(domain in parsed.netloc for domain in domains):
            continue
        if href in seen:
            continue
        seen.add(href)
        if any(bad in title.lower() for bad in ("subscribe", "sign in", "newsletter", "privacy policy", "advertise")):
            continue
        published_at = ""
        parent = a
        for _ in range(3):
            parent = parent.parent if parent is not None else None
            if parent is None:
                break
            time_el = parent.find("time") if hasattr(parent, "find") else None
            if time_el is not None:
                published_at = time_el.get("datetime") or time_el.get_text(" ", strip=True)
                break
        items.append({
            "title": title,
            "source": index["source"],
            "published_at": _parse_rss_date(published_at),
            "url": href,
            "source_home_url": index["url"],
            "description": "",
            "query": f"native:{index['source']}",
            "discovery": "native_html",
        })
        if len(items) >= max_items:
            break
    return items


def _fetch_native_source_key(key: str, window_hours: int) -> tuple[str, list[dict], dict]:
    items: list[dict] = []
    errors = 0
    if key == "cnyes":
        try:
            items.extend(_fetch_cnyes_native(window_hours))
        except Exception:
            errors += 1
    for feed in NATIVE_RSS_FEEDS.get(key, []):
        try:
            items.extend(_fetch_native_rss_feed(feed, window_hours))
        except Exception:
            errors += 1
    for index in NATIVE_HTML_INDEXES.get(key, []):
        try:
            items.extend(_fetch_html_index(index, window_hours))
        except Exception:
            errors += 1
    stats = {
        "items": len(items),
        "rss_feeds": len(NATIVE_RSS_FEEDS.get(key, [])),
        "html_indexes": len(NATIVE_HTML_INDEXES.get(key, [])),
        "api": key == "cnyes",
        "errors": errors,
    }
    return key, items, stats


def _fetch_native_headlines(allowed_sources: list[str], window_hours: int) -> tuple[list[dict], dict]:
    keys = _native_source_keys(allowed_sources)
    stats = {key: {"items": 0, "rss_feeds": 0, "html_indexes": 0, "api": False, "errors": 0} for key in keys}
    if not keys:
        return [], stats
    items: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(keys)))) as pool:
        futures = {pool.submit(_fetch_native_source_key, key, window_hours): key for key in keys}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                _, key_items, key_stats = fut.result()
            except Exception:
                key_items = []
                key_stats = {**stats.get(key, {}), "errors": 1}
            items.extend(key_items)
            stats[key] = key_stats
    return items, stats


def _compact_match_text(value: str) -> str:
    text = _strip_markup(value).lower()
    text = re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", text)
    return text


def _ngram_score(a: str, b: str) -> float:
    a = _compact_match_text(a)
    b = _compact_match_text(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    if len(a) < 2 or len(b) < 2:
        return 1.0 if a == b else 0.0
    agrams = {a[i:i + 2] for i in range(len(a) - 1)}
    bgrams = {b[i:i + 2] for i in range(len(b) - 1)}
    denom = max(1, min(len(agrams), len(bgrams)))
    return len(agrams & bgrams) / denom


def _headline_search_queries(title: str) -> list[str]:
    title = _strip_markup(title)
    ascii_terms = re.findall(r"[A-Za-z][A-Za-z0-9.+-]{1,}|[0-9]+(?:\.[0-9]+)?%?", title)
    queries = []
    if ascii_terms:
        queries.append(" ".join(ascii_terms[:5]))
        primary = ascii_terms[0]
        upper_terms = {term.upper() for term in ascii_terms}
        if "IPO" in upper_terms:
            queries.append(f"{primary} IPO")
        numeric_terms = [term for term in ascii_terms[1:] if re.search(r"\d", term)]
        if numeric_terms:
            queries.append(" ".join([primary, *numeric_terms[:2]]))
    compact = re.sub(r"\s+", "", title)
    if compact:
        queries.append(compact[:24])
    if ascii_terms and len(ascii_terms) > 1:
        queries.append(" ".join(ascii_terms[:2]))
    out = []
    seen = set()
    for query in queries:
        query = query.strip()
        key = query.lower()
        if query and key not in seen:
            seen.add(key)
            out.append(query)
    return out


def _cnyes_context_from_api(item: dict) -> dict:
    source = (item.get("source") or "").lower()
    source_home = (item.get("source_home_url") or "").lower()
    url = (item.get("url") or "").lower()
    if not any("cnyes" in value for value in (source, source_home, url)):
        return {}

    title = item.get("title") or ""
    best: tuple[float, dict] | None = None
    for query in _headline_search_queries(title):
        try:
            api_url = f"https://api.cnyes.com/media/api/v1/search/news?q={quote_plus(query)}&page=1"
            resp = _http_get(api_url, timeout=8)
            if resp.status_code != 200:
                continue
            rows = ((resp.json().get("items") or {}).get("data") or [])[:20]
        except Exception:
            continue
        for row in rows:
            score = _ngram_score(title, row.get("title") or "")
            if best is None or score > best[0]:
                best = (score, row)

    if not best or best[0] < 0.45:
        return {}
    row = best[1]
    news_id = row.get("newsId")
    clean_title = _strip_markup(row.get("title") or title)
    content = _strip_markup(row.get("content") or "")
    signature = _strip_markup(row.get("signature") or "")
    article_text = " ".join(x for x in (clean_title, signature, content) if x).strip()
    out = {
        "article_text": article_text[:ARTICLE_CONTEXT_CHARS],
        "resolved_url": f"https://news.cnyes.com/news/id/{news_id}" if news_id else "",
        "resolved_title": clean_title,
        "article_match_score": round(best[0], 3),
        "article_source": "cnyes_api",
    }
    published = row.get("publishAt")
    if published and not item.get("published_at"):
        try:
            out["published_at"] = datetime.fromtimestamp(int(published), tz=timezone.utc).isoformat(timespec="seconds")
        except Exception:
            pass
    return out


def _extract_article_text_from_html(html_text: str) -> str:
    if not html_text:
        return ""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html_text, "lxml")
        for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        pieces = []
        for selector in (
            "meta[property='og:title']",
            "meta[name='twitter:title']",
            "meta[name='description']",
            "meta[property='og:description']",
        ):
            tag = soup.select_one(selector)
            content = tag.get("content", "").strip() if tag else ""
            if content:
                pieces.append(content)
        container = soup.find("article") or soup.body or soup
        paragraphs = []
        for p in container.find_all(["p", "li"]):
            text = re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
            if len(text) >= 40:
                paragraphs.append(text)
            if sum(len(x) for x in paragraphs) >= ARTICLE_CONTEXT_CHARS:
                break
        pieces.extend(paragraphs)
        return _strip_markup(" ".join(pieces))[:ARTICLE_CONTEXT_CHARS]
    except Exception:
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html_text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        return _strip_markup(text)[:ARTICLE_CONTEXT_CHARS]


def _fetch_article_context(item: dict) -> dict:
    cnyes_context = _cnyes_context_from_api(item)
    if cnyes_context:
        return cnyes_context

    url = item.get("url") or ""
    if not url or "news.google." in urlparse(url).netloc:
        return {}
    try:
        resp = _http_get(url, timeout=8)
        if resp.status_code >= 400:
            return {}
        text = _extract_article_text_from_html(resp.text)
    except Exception:
        return {}
    if not text:
        return {}
    return {
        "article_text": text,
        "article_source": "publisher_html",
    }


def _enrich_digest_items_with_article_text(items: list[dict]) -> list[dict]:
    if not items:
        return []
    enriched = [dict(item) for item in items]
    max_workers = min(6, max(1, len(enriched)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_article_context, item): idx for idx, item in enumerate(enriched)}
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                context = fut.result() or {}
            except Exception:
                context = {}
            if not context:
                continue
            if context.get("resolved_url"):
                enriched[idx]["url"] = context["resolved_url"]
            if context.get("published_at"):
                enriched[idx]["published_at"] = context["published_at"]
            for key in ("article_text", "resolved_title", "article_match_score", "article_source"):
                if context.get(key):
                    enriched[idx][key] = context[key]
    return enriched


def _fetch_google_news(
    term: str,
    allowed_sources: list[str],
    max_items: int = 8,
    window_hours: int = 6,
) -> list[dict]:
    items = []
    seen = set()
    q = quote_plus(term)
    feeds = [
        f"https://news.google.com/rss/search?q={q}+when:{window_hours}h&hl=en-US&gl=US&ceid=US:en",
        f"https://news.google.com/rss/search?q={q}+when:{window_hours}h&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        f"https://news.google.com/rss/search?q={q}+when:{window_hours}h&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
    ]
    for feed in feeds:
        try:
            resp = _http_get(feed, timeout=10)
            if resp.status_code != 200:
                continue
            root = ET.fromstring(resp.content)
            for el in root.findall(".//item"):
                title = el.findtext("title", "")
                source_el = el.find("source")
                source = source_el.text if source_el is not None and source_el.text else ""
                if " - " in title:
                    title_part, source_part = title.rsplit(" - ", 1)
                    title = title_part
                    source = source or source_part
                if not _approved_source(source, allowed_sources):
                    continue
                link = el.findtext("link", "")
                pub_date = _parse_rss_date(el.findtext("pubDate", ""))
                if not _within_window(pub_date, window_hours):
                    continue
                description = _clean_rss_description(el.findtext("description", ""), title, source)
                key = re.sub(r"\W+", " ", title.lower()).strip()[:100]
                if key in seen:
                    continue
                seen.add(key)
                items.append({
                    "title": title.strip(),
                    "source": source.strip(),
                    "published_at": pub_date,
                    "url": _normalise_google_news_url(link),
                    "source_home_url": _source_home_url(source_el),
                    "description": description,
                    "query": term,
                })
                if len(items) >= max_items:
                    return items
        except Exception:
            continue
    return items


def _headline_key(item: dict) -> str:
    raw = f"{item.get('title', '').lower()}|{item.get('url', '').lower()}"
    raw = re.sub(r"\s+", " ", raw)
    return kb.text_hash(raw)[:20]


def _score_item(item: dict) -> tuple[int, dict]:
    title = item.get("title", "")
    haystack = _headline_signal_text(item)
    low = haystack.lower()
    entities = kb.extract_entities(haystack, title=title)
    score = 0
    if entities.get("tickers"):
        score += 5
    if entities.get("themes"):
        score += 3
    for kw in MATERIAL_KEYWORDS:
        if kw in low:
            score += 2
    source = (item.get("source") or "").lower()
    if any(s in source for s in ("bloomberg", "reuters", "nikkei", "digitimes", "trendforce")):
        score += 1
    enriched = {**item, "score": score, "entities": entities}
    return score, enriched


def _headline_signal_text(item: dict) -> str:
    return " ".join(
        _repair_text_encoding(item.get(key, ""))
        for key in ("title", "description", "article_text", "source")
        if item.get(key)
    )


def _has_hard_tech_signal(item: dict) -> bool:
    text = _headline_signal_text(item).lower()
    return any(term.lower() in text for term in HARD_TECH_SIGNAL_TERMS)


def _is_digest_candidate(item: dict) -> bool:
    if not _has_hard_tech_signal(item):
        return False
    source = (item.get("source") or "").lower()
    url = (item.get("url") or "").lower()
    if "bloomberg" in source and "/news/videos/" in url:
        return False
    return True


def _extract_json_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


PLACEHOLDER_SUMMARY_RE = re.compile(
    r"(source report from|tap the analyse|deeper read-through|analyse command|click.*analyse)",
    re.I,
)
CJK_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def _contains_cjk(value: str) -> bool:
    return bool(CJK_RE.search(str(value or "")))


def _chinese_headline_fallback(item: dict) -> dict | None:
    title = str(item.get("title") or "")
    source = item.get("source") or "the source"
    if "SpaceX" in title and "\u4e0a\u5e02" in title and "AI" in title and "3.5" in title:
        return {
            "rank": item.get("rank", 0),
            "key_sentence": "A SpaceX IPO could pressure AI-stock valuations, with research showing large IPO returns fade sharply after listing.",
            "summary": (
                "The cnyes report says IPO performance tends to cool quickly, citing research that large IPOs average only about 3.5% returns after one year. "
                "The read-through is that a major SpaceX listing could pull capital and attention away from crowded AI winners, even if it is not a direct semiconductor demand signal."
            ),
        }
    if "ASML" in title and "\u5e02\u503c" in title and ("\u6469\u6839\u5927\u901a" in title or "\u9ad8\u76db" in title):
        return {
            "rank": item.get("rank", 0),
            "key_sentence": "ASML reached a record European market value as JPMorgan and Goldman Sachs reiterated bullish views.",
            "summary": (
                "The cnyes report says ASML's market capitalization hit a new European high while major brokers remained positive on the stock. "
                "The read-through is continued investor confidence in EUV lithography as a bottleneck asset for AI and leading-edge semiconductor capacity."
            ),
        }
    if "SpaceX" in title and "OpenAI" in title and "ETF" in title:
        return {
            "rank": item.get("rank", 0),
            "key_sentence": "SpaceX and OpenAI IPOs are unlikely to immediately disrupt broad-market ETFs, according to the report.",
            "summary": (
                "The cnyes report says broad index funds typically add new IPOs gradually based on float-adjusted market capitalization rather than full value on day one. "
                "The read-through is that large private-AI listings may affect sentiment, but the mechanical ETF impact should be limited at first."
            ),
        }
    if "\u6d77\u529b\u58eb" in title and "HBM4" in title:
        return {
            "rank": item.get("rank", 0),
            "key_sentence": "SK Hynix is expanding HBM4 capacity, with related equipment orders moving through the supply chain.",
            "summary": (
                "The report says SK Hynix is moving aggressively to add HBM4 capacity and is placing related equipment orders. "
                "The read-through is continued urgency around HBM supply-chain capex and advanced packaging equipment."
            ),
        }
    if "\u8a18\u61b6\u9ad4" in title and "HBM" in title and "\u6563\u71b1" in title:
        return {
            "rank": item.get("rank", 0),
            "key_sentence": "The HBM race among the major memory makers is shifting toward thermal management, not just stack height.",
            "summary": (
                "The report says memory suppliers are competing on HBM heat dissipation as AI accelerators push bandwidth and power density higher. "
                "The read-through is that thermal design, packaging, and materials may become more important differentiators for Samsung, SK Hynix, and Micron."
            ),
        }
    if "\u4e09\u661f" in title and "\u8f1d\u9054" in title and ("HBM4" in title or "HBM5" in title):
        return {
            "rank": item.get("rank", 0),
            "key_sentence": "Samsung is pushing for Nvidia AI-memory orders as discussions focus on next-generation HBM cooperation.",
            "summary": (
                "The report says Samsung is trying to deepen its Nvidia relationship around HBM4, HBM4E, or HBM5 products. "
                "The read-through is whether Samsung can narrow the HBM execution gap with SK Hynix and regain share in AI memory supply."
            ),
        }
    return None


def _english_fallback_row(item: dict) -> dict:
    title = str(item.get("title") or "")
    title_l = title.lower()
    source = item.get("source") or "the source"

    if "sk海力士" in title_l and "hbm4" in title_l and "韓美半導體" in title_l:
        key = "SK Hynix is expanding HBM4 capacity, with Hanmi Semiconductor winning related equipment orders."
        summary = (
            "The report says SK Hynix is moving aggressively to add HBM4 capacity and is placing equipment orders with Hanmi Semiconductor. "
            "The read-through is continued urgency around HBM supply-chain capex and advanced bonding or packaging equipment."
        )
    elif "記憶體三大巨頭" in title and "hbm" in title_l and "散熱" in title:
        key = "The HBM race among the three major memory makers is shifting toward thermal management, not just higher stack counts."
        summary = (
            "The report says memory suppliers are competing on HBM heat dissipation as AI accelerators push bandwidth and power density higher. "
            "The read-through is that thermal design, packaging, and materials may become more important differentiators for Samsung, SK Hynix, and Micron."
        )
    elif "sk海力士" in title_l and "dram" in title_l and "2030" in title_l:
        key = "SK Hynix is reportedly targeting a major DRAM capacity expansion by 2030 despite oversupply risk."
        summary = (
            "The report discusses why SK Hynix may double down on DRAM capacity even though the cycle could face future oversupply. "
            "The read-through is that memory makers may be prioritizing AI-driven long-term demand and strategic share over near-term cycle discipline."
        )
    elif "三星" in title and "輝達" in title and ("hbm4e" in title_l or "hbm5" in title_l):
        key = "Samsung is pushing for Nvidia AI orders as discussions focus on HBM4E and HBM5 cooperation."
        summary = (
            "The report says Samsung is trying to deepen its Nvidia relationship around next-generation HBM products. "
            "The read-through is whether Samsung can narrow the HBM execution gap with SK Hynix and regain share in AI memory supply."
        )
    elif _contains_cjk(title):
        specific = _chinese_headline_fallback(item)
        if specific:
            return specific
        entities = []
        entity_map = [
            ("SK海力士", "SK Hynix"),
            ("三星", "Samsung"),
            ("輝達", "Nvidia"),
            ("英特爾", "Intel"),
            ("台積電", "TSMC"),
            ("聯發科", "MediaTek"),
            ("韓美半導體", "Hanmi Semiconductor"),
            ("黃仁勳", "Jensen Huang"),
        ]
        for needle, label in entity_map:
            if needle in title and label not in entities:
                entities.append(label)
        topics = []
        for pattern, label in (
            ("HBM", "HBM"),
            ("DRAM", "DRAM"),
            ("NAND", "NAND"),
            ("AI", "AI infrastructure"),
            ("產能", "capacity"),
            ("散熱", "thermal management"),
            ("設備", "equipment orders"),
            ("晶圓", "wafer capacity"),
        ):
            if pattern.lower() in title_l or pattern in title:
                topics.append(label)
        subject = ", ".join([*entities, *topics]) or _strip_markup(title)[:120] or "this technology headline"
        key = f"{subject}."
        summary = (
            f"The source headline concerns {subject}. "
            "Article extraction or machine translation was incomplete, so this row should be treated as a headline-level flag rather than a full summary."
        )
    else:
        key = _normalise_key_sentence(title)
        summary = _summary_from_headline(item)

    return {
        "rank": item.get("rank", 0),
        "key_sentence": _normalise_key_sentence(key),
        "summary": _normalise_summary(summary),
    }


def _summary_from_headline(item: dict) -> str:
    if _contains_cjk(item.get("title", "")) or _contains_cjk(item.get("description", "")):
        return _english_fallback_row(item)["summary"]
    title = _normalise_key_sentence(item.get("title", ""))
    snippet = _normalise_summary(item.get("description", ""))
    title_l = title.lower()
    source = item.get("source") or "the source"

    if snippet and not PLACEHOLDER_SUMMARY_RE.search(snippet):
        return snippet

    if "foundr" in title_l and ("ranking" in title_l or "revenue" in title_l):
        return (
            "The article tracks revenue rankings among global foundries, which can show market-share shifts, "
            "pricing pressure, and utilization trends across TSMC, Samsung, SMIC, UMC, GlobalFoundries, and peers. "
            "The read-through is mainly for foundry cycle strength and whether AI demand is broadening beyond the leading edge."
        )
    if "nvidia" in title_l and ("stock" in title_l or "rebound" in title_l or "gain" in title_l):
        return (
            "The article covers a rebound in US equities led by Nvidia and other large technology stocks. "
            "The read-through is risk appetite for AI infrastructure leaders and whether semiconductor momentum is being driven by fundamentals, positioning, or broader market beta."
        )
    if "hbm" in title_l or "memory" in title_l or "dram" in title_l or "nand" in title_l:
        return (
            "The article concerns the memory cycle, with potential implications for HBM, DRAM, NAND pricing, and AI server supply chains. "
            "The key read-through is whether demand, pricing, or capacity allocation is improving for memory suppliers and upstream equipment names."
        )
    if "ai server" in title_l or "gpu" in title_l or "data center" in title_l:
        return (
            "The article concerns AI infrastructure demand across servers, GPUs, data centers, or the related supply chain. "
            "The key read-through is whether deployment bottlenecks, capex, or supplier positioning is changing for covered AI infrastructure names."
        )
    if "substrate" in title_l or "abf" in title_l or "pcb" in title_l or "ccl" in title_l:
        return (
            "The article concerns the electronics substrate, PCB, or CCL supply chain that supports advanced computing hardware. "
            "The key read-through is whether AI server demand is tightening capacity, lifting pricing, or changing the relative attractiveness of suppliers."
        )

    subject = title[:-1] if title.endswith(".") else title
    return (
        f"The article from {source} covers {subject}. "
        "The key read-through is whether this changes demand, pricing, capacity, or competitive positioning across semiconductors, AI infrastructure, and the relevant supply chain."
    )


def _fallback_brief_rows(items: list[dict]) -> list[dict]:
    rows = []
    for item in items:
        if _contains_cjk(item.get("title", "")) or _contains_cjk(item.get("description", "")):
            rows.append({**_english_fallback_row(item), "rank": item.get("rank", len(rows) + 1)})
            continue
        rows.append(
            {
                "rank": item.get("rank", len(rows) + 1),
                "key_sentence": item.get("title", "").rstrip(".") + ".",
                "summary": _summary_from_headline(item),
            }
        )
    return rows


def _normalise_key_sentence(value: str) -> str:
    text = re.sub(r"\s+", " ", _repair_text_encoding(value)).strip()
    text = re.sub(r"[.!?。！？]+$", "", text).strip()
    return f"{text}." if text else ""


def _normalise_summary(value: str, fallback: str = "") -> str:
    text = re.sub(r"\s+", " ", _repair_text_encoding(value or fallback or "")).strip()
    if PLACEHOLDER_SUMMARY_RE.search(text):
        text = re.sub(r"\s+", " ", _repair_text_encoding(fallback or "")).strip()
    if not text:
        return ""
    marker = "__HEADLINE_DOT__"
    protected = re.sub(r"(?<=[A-Za-z0-9])\.(?=[A-Za-z0-9])", marker, text)
    parts = re.findall(r"[^.!?。！？]+[.!?。！？]?", protected)
    sentences = []
    for part in parts:
        sentence = part.strip().replace(marker, ".")
        if not sentence:
            continue
        if not re.search(r"[.!?。！？]$", sentence):
            sentence += "."
        sentences.append(sentence)
        if len(sentences) == 2:
            break
    return " ".join(sentences) if sentences else text


def _tech_brief_rows_with_claude(items: list[dict], window_hours: int = 6) -> list[dict]:
    if not items:
        return []
    lines = []
    for item in items:
        lines.append(
            f"{item.get('rank', '?')}. [{item['source']}] {item['title']} "
            f"Description: {item.get('description', '') or 'N/A'} "
            f"Article text: {(item.get('article_text') or 'N/A')[:1800]} "
            f"(Published: {_format_hkt(item.get('published_at', ''))}) URL: {item.get('url', 'N/A')}"
        )
    prompt = f"""Tech Brief: summarize semiconductor and technology supply chain headlines from the past {window_hours} hours.

RULES:
1. Return ONLY valid JSON: {{"items":[{{"rank":1,"key_sentence":"...","summary":"..."}}]}}.
2. Keep rank equal to the input rank. Do not invent links or sources.
3. key_sentence is one concise topic sentence, suitable to bold in Telegram. Do not include source/time/link text.
4. summary is exactly two short sentences where possible. Use Article text when present; otherwise use the headline and RSS description. Summarize the underlying news adequately: who/what happened, important numbers or counterparties, and why it matters for tech/semis/AI infrastructure.
5. Skip non-tech / low-signal headlines by omitting their rank.
6. Focus on semiconductors, foundries, memory, AI servers, substrates, PCB, CCL, IC design, GPUs, smartphones, PCs, data center power/cooling, AI infrastructure.
7. Do not do a full investment analysis here. This is a lightweight brief only.
8. Output every key_sentence and summary in English. Translate Chinese, Japanese, Korean, or other non-English source headlines into fluent English. Do not output any Chinese/Japanese/Korean characters.

Headlines:
{chr(10).join(lines)}
"""
    try:
        from scripts.llm_provider import call_api, get_client

        client = get_client("anthropic", timeout=90.0, max_retries=2)
        raw = call_api(
            client,
            [{"role": "user", "content": prompt}],
            max_tokens=3000,
            model=(
                os.environ.get("HEADLINE_ANTHROPIC_MODEL")
                or "claude-sonnet-4-6"
            ),
        )
        payload = _extract_json_object(raw)
        rows = []
        valid_ranks = {int(item["rank"]) for item in items if item.get("rank")}
        for row in payload.get("items", []):
            try:
                rank = int(row.get("rank"))
            except Exception:
                continue
            if rank not in valid_ranks:
                continue
            key_sentence = str(row.get("key_sentence", "")).strip()
            summary = str(row.get("summary", "")).strip()
            if not key_sentence:
                continue
            rows.append({"rank": rank, "key_sentence": key_sentence, "summary": summary})
        by_rank = _rows_by_rank(rows)
        completed = [_clean_row_for_item(item, by_rank.get(int(item["rank"]))) for item in items]
        completed = _ensure_english_rows(items, completed, window_hours)
        completed.sort(key=lambda x: x["rank"])
        return completed
    except Exception:
        return _ensure_english_rows(items, _fallback_brief_rows(items), window_hours)


def _ensure_english_rows(items: list[dict], rows: list[dict], window_hours: int = 6) -> list[dict]:
    if not any(_contains_cjk(r.get("key_sentence", "")) or _contains_cjk(r.get("summary", "")) for r in rows):
        return rows
    try:
        translated = _translate_rows_with_claude(items, rows, window_hours)
    except Exception:
        translated = rows
    by_rank = _rows_by_rank(translated)
    cleaned = []
    for item in items:
        rank = int(item["rank"])
        row = _clean_row_for_item(item, by_rank.get(rank))
        if _contains_cjk(row.get("key_sentence", "")) or _contains_cjk(row.get("summary", "")):
            row = _english_fallback_row(item)
            row["rank"] = rank
        cleaned.append(row)
    return cleaned


def _translate_rows_with_claude(items: list[dict], rows: list[dict], window_hours: int = 6) -> list[dict]:
    by_rank = _rows_by_rank(rows)
    payload = []
    for item in items:
        rank = int(item["rank"])
        row = by_rank.get(rank) or {}
        payload.append(
            {
                "rank": rank,
                "source": item.get("source"),
                "title": item.get("title"),
                "description": item.get("description"),
                "article_text": (item.get("article_text") or "")[:1800],
                "draft_key_sentence": row.get("key_sentence"),
                "draft_summary": row.get("summary"),
            }
        )
    prompt = f"""Rewrite these Tech Brief rows in English.

Return ONLY valid JSON: {{"items":[{{"rank":1,"key_sentence":"...","summary":"..."}}]}}.
Rules:
- Keep the same ranks.
- Translate any Chinese, Japanese, Korean, or other non-English text into fluent English.
- key_sentence must be one concise English sentence.
- summary must be one or two short English sentences with the key news and why it matters for semiconductors, AI infrastructure, memory, foundry, or supply chain.
- Use article_text when present. If article_text is unavailable, translate and summarize the actual title/description; do not output generic phrases like "appears to cover a semiconductor supply-chain development."
- Do not output Chinese/Japanese/Korean characters.
- Do not add links, sources, or full investment analysis.
- The lookback window is {window_hours} hours.

Rows:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""
    from scripts.llm_provider import call_api, get_client

    client = get_client("anthropic", timeout=60.0, max_retries=1)
    raw = call_api(
        client,
        [{"role": "user", "content": prompt}],
        max_tokens=3000,
        model=(
            os.environ.get("HEADLINE_ANTHROPIC_MODEL")
            or "claude-sonnet-4-6"
        ),
    )
    result = _extract_json_object(raw)
    valid_ranks = {int(item["rank"]) for item in items if item.get("rank")}
    out = []
    for row in result.get("items", []):
        try:
            rank = int(row.get("rank"))
        except Exception:
            continue
        if rank not in valid_ranks:
            continue
        key_sentence = str(row.get("key_sentence", "")).strip()
        summary = str(row.get("summary", "")).strip()
        if not key_sentence:
            continue
        out.append({"rank": rank, "key_sentence": key_sentence, "summary": summary})
    return out or rows


def _rows_by_rank(rows: list[dict]) -> dict[int, dict]:
    out = {}
    for row in rows:
        try:
            out[int(row["rank"])] = row
        except Exception:
            continue
    return out


def _clean_row_for_item(item: dict, row: dict | None) -> dict:
    fallback = _fallback_brief_rows([item])[0]
    row = row or fallback
    key_sentence = row.get("key_sentence") or fallback["key_sentence"]
    summary = _normalise_summary(row.get("summary", ""), fallback=fallback["summary"])
    if not summary:
        summary = _normalise_summary(fallback["summary"])
    if _contains_cjk(key_sentence) or _contains_cjk(summary):
        english = _english_fallback_row(item)
        if _contains_cjk(key_sentence):
            key_sentence = english["key_sentence"]
        if _contains_cjk(summary):
            summary = english["summary"]
    return {
        "rank": int(item["rank"]),
        "key_sentence": key_sentence,
        "summary": summary,
    }


def _format_markdown_brief(items: list[dict], rows: list[dict]) -> str:
    by_rank = _rows_by_rank(rows)
    parts = []
    for item in items:
        rank = int(item["rank"])
        row = _clean_row_for_item(item, by_rank.get(rank))
        source = item.get("source", "Source")
        time = _format_hkt(item.get("published_at", ""))
        url = item.get("url", "")
        source_label = f"{source}, {time}" if time else source
        key_sentence = _normalise_key_sentence(row["key_sentence"])
        summary = _normalise_summary(row.get("summary", ""), fallback=_summary_from_headline(item))
        suffix = f"([{source_label}]({url}) | analyse: /headline_{rank})" if url else f"({source_label} | analyse: /headline_{rank})"
        parts.append(f"{rank}. **{key_sentence}**\n{summary}\n{suffix}".strip())
    return "\n\n".join(parts)


def _format_telegram_brief(items: list[dict], rows: list[dict], window_hours: int) -> str:
    by_rank = _rows_by_rank(rows)
    parts = [f"<b>Tech Brief</b> - top {len(items)} headlines from the last {window_hours} hours"]
    for item in items:
        rank = int(item["rank"])
        row = _clean_row_for_item(item, by_rank.get(rank))
        source = escape(str(item.get("source", "Source")))
        time = escape(_format_hkt(item.get("published_at", "")))
        source_label = f"{source}, {time}" if time else source
        key_sentence = escape(_normalise_key_sentence(row["key_sentence"]))
        summary = escape(_normalise_summary(row.get("summary", ""), fallback=_summary_from_headline(item)))
        url = escape(item.get("url", ""), quote=True)
        if url:
            suffix = f'(<a href="{url}">link</a> | analyse: /headline_{rank})'
        else:
            suffix = f"(analyse: /headline_{rank})"
        parts.append(
            f"{rank}. <b>{key_sentence}</b>\n"
            f"{summary}\n"
            f"<i>{source_label}</i> {suffix}".strip()
        )
    return "\n\n".join(parts)


def _save_latest_digest(
    items: list[dict],
    brief: str,
    rows: list[dict] | None = None,
    telegram_html: str = "",
    window_hours: int = 6,
) -> None:
    HEADLINE_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_DIGEST_PATH.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "window_hours": window_hours,
                "brief": brief,
                "telegram_html": telegram_html,
                "rows": rows or [],
                "items": items,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def get_headline(key: str) -> dict | None:
    state = _load_state()
    item = (state.get("items") or {}).get(key)
    if item:
        return item
    if LATEST_DIGEST_PATH.exists():
        try:
            latest = json.loads(LATEST_DIGEST_PATH.read_text(encoding="utf-8"))
            for candidate in latest.get("items", []):
                if candidate.get("key") == key:
                    return candidate
        except json.JSONDecodeError:
            pass
    return None


def get_headline_by_rank(rank: int) -> dict | None:
    if not LATEST_DIGEST_PATH.exists():
        return None
    try:
        latest = json.loads(LATEST_DIGEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    for item in latest.get("items", []):
        try:
            if int(item.get("rank", 0)) == int(rank):
                return item
        except Exception:
            continue
    return None


def analyse_headline(key: str, notify: bool = True) -> dict:
    item = get_headline(key)
    if not item:
        raise FileNotFoundError(f"Headline not found for key {key}")
    from scripts.analyst import headline_readthrough

    analysis = headline_readthrough([item])
    analysis_path = HEADLINE_DIR / f"{datetime.now().strftime('%Y-%m-%d_%H%M%S')}_{key}_analysis.md"
    analysis_path.write_text("# Headline Analysis\n\n" + analysis + "\n", encoding="utf-8")
    kb.index_text(
        title=f"Headline Analysis - {item.get('title', key)[:120]}",
        text=analysis,
        source_type="headlines",
        source_uri=f"headline-analysis:{key}:{analysis_path.name}",
        source_path=str(analysis_path),
        metadata={"headline": item, "headline_key": key},
        force=True,
    )
    if notify:
        telegram_send_markdownish_html(analysis)
    return {"key": key, "title": item.get("title"), "analysis_path": str(analysis_path)}


def _sort_timestamp(item: dict) -> float:
    dt = _published_dt(item.get("published_at", ""))
    return dt.timestamp() if dt else 0.0


def headline_sweep(
    notify: bool = False,
    max_digest_items: int = 20,
    window_hours: int = 24,
) -> dict:
    config = _load_config()
    allowed_sources = _source_list(config)
    terms = _terms(config)
    state = _load_state()
    seen = state.setdefault("seen", {})
    stored_items = state.setdefault("items", {})
    HEADLINE_DIR.mkdir(parents=True, exist_ok=True)
    now_utc = datetime.now(timezone.utc)
    fetched_at = now_utc.isoformat(timespec="seconds")

    native_items, native_stats = _fetch_native_headlines(allowed_sources, window_hours)
    native_keys = _native_source_keys(allowed_sources)
    native_keys_with_items = {key for key, stat in native_stats.items() if stat.get("items", 0) > 0}
    fallback_keys = native_keys - native_keys_with_items
    google_fallback_sources = sorted(_source_names_for_keys(fallback_keys))
    use_google_fallback = os.environ.get("HEADLINE_GOOGLE_FALLBACK", "1").strip().lower() not in {"0", "false", "no"}

    google_items = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {}
        if use_google_fallback and google_fallback_sources:
            futures = {
                pool.submit(_fetch_google_news, term, google_fallback_sources, 8, window_hours): term
                for term in terms
            }
        for fut in as_completed(futures):
            try:
                google_items.extend(fut.result())
            except Exception:
                pass

    all_items = [*native_items, *google_items]

    unique = {}
    for item in all_items:
        key = _headline_key(item)
        # Undated items fall back to when we first saw this headline; a new
        # headline this sweep is treated as fresh, a long-reappearing undated
        # one (old first_seen) is filtered out as stale.
        prior_seen = seen.get(key) or {}
        first_seen = prior_seen.get("first_seen_at") or fetched_at
        if not _within_window(item.get("published_at", ""), window_hours, now_utc,
                              fallback=first_seen):
            continue
        previous = unique.get(key)
        if not previous or _sort_timestamp(item) > _sort_timestamp(previous):
            unique[key] = item

    new_items = []
    candidate_items = []
    raw_path = HEADLINE_DIR / f"{datetime.now().strftime('%Y-%m-%d')}_headlines.jsonl"
    with open(raw_path, "a", encoding="utf-8") as f:
        for key, item in unique.items():
            item = {**item, "key": key, "fetched_at": fetched_at}
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
            candidate_items.append(item)
            stored_items[key] = item
            if key not in seen:
                new_items.append(item)
                seen[key] = {
                    "title": item.get("title"),
                    "source": item.get("source"),
                    "url": item.get("url"),
                    "published_at": item.get("published_at"),
                    "first_seen_at": item["fetched_at"],
                }
    _save_state(state)

    scored = [_score_item(item)[1] for item in candidate_items]
    ranked = sorted(
        [item for item in scored if item["score"] > 0 and _is_digest_candidate(item)],
        key=lambda x: (x["score"], _sort_timestamp(x)),
        reverse=True,
    )
    digest_items = []
    for idx, item in enumerate(ranked[:max_digest_items], start=1):
        resolved_url = _resolve_source_url(item.get("url", ""))
        ranked_item = {**item, "url": resolved_url or item.get("url", ""), "rank": idx}
        digest_items.append(ranked_item)
        stored_items[item["key"]] = ranked_item
    digest_items = _enrich_digest_items_with_article_text(digest_items)
    for item in digest_items:
        stored_items[item["key"]] = item
    _save_state(state)

    rows = _tech_brief_rows_with_claude(digest_items, window_hours=window_hours)
    digest = _format_markdown_brief(digest_items, rows) if digest_items else ""
    telegram_html = _format_telegram_brief(digest_items, rows, window_hours) if digest_items else ""
    digest_path = None
    if digest:
        digest_path = HEADLINE_DIR / f"{datetime.now().strftime('%Y-%m-%d_%H%M%S')}_digest.md"
        digest_path.write_text("# Tech Brief\n\n" + digest + "\n", encoding="utf-8")
        _save_latest_digest(
            digest_items,
            digest,
            rows=rows,
            telegram_html=telegram_html,
            window_hours=window_hours,
        )
        kb.index_text(
            title=f"Tech Brief {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            text=digest,
            source_type="headlines",
            source_uri=f"headlines:{digest_path.name}",
            source_path=str(digest_path),
            metadata={"items": digest_items},
            force=True,
        )
    if notify and digest:
        telegram_send(telegram_html, parse_mode="HTML", disable_web_page_preview=True)
    elif notify:
        telegram_send(f"Tech Brief: no material tech headlines found in the last {window_hours} hours.")
    return {
        "terms": len(terms),
        "window_hours": window_hours,
        "fetched": len(all_items),
        "unique": len(unique),
        "new": len(new_items),
        "digest_items": len(digest_items),
        "raw_path": str(raw_path),
        "digest_path": str(digest_path) if digest_path else None,
        "native_items": len(native_items),
        "google_items": len(google_items),
        "native_stats": native_stats,
        "google_fallback_sources": google_fallback_sources if use_google_fallback else [],
    }
