"""Headline sweep, dedupe, materiality ranking, and Telegram digest."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html import escape, unescape
from pathlib import Path
from urllib.parse import quote_plus, urlparse
from zoneinfo import ZoneInfo

import requests

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


def _save_state(state: dict) -> None:
    HEADLINE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _source_list(config: dict) -> list[str]:
    feed_sources = config.get("feed_sources", {})
    configured = []
    for key in ("headlines", "topics", "portfolio", "tech"):
        configured.extend(feed_sources.get(key, []))
    return [s.lower() for s in configured] or DEFAULT_SOURCES


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


def _within_window(value: str, window_hours: int, now: datetime | None = None) -> bool:
    dt = _published_dt(value)
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


def _resolve_source_url(url: str) -> str:
    """Best-effort conversion of Google News links into the publisher article URL."""
    if not url:
        return ""
    parsed = urlparse(url)
    if "news.google." not in parsed.netloc:
        return url
    try:
        resp = requests.get(
            url,
            timeout=8,
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        final_url = resp.url or url
        if "news.google." not in urlparse(final_url).netloc:
            return final_url
    except Exception:
        pass
    return url


def _clean_html_fragment(value: str) -> str:
    text = unescape(str(value or ""))
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
            resp = requests.get(feed, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
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
    low = title.lower()
    entities = kb.extract_entities(title, title=title)
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
        subject = ", ".join([*entities, *topics]) or "a semiconductor supply-chain development"
        key = f"A Chinese-language report discusses {subject}."
        summary = (
            f"The article from {source} appears to cover {subject} in the technology supply chain. "
            "The read-through is whether this changes demand, capacity, pricing, or competitive positioning for covered semiconductor and AI infrastructure names."
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
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"[.!?。！？]+$", "", text).strip()
    return f"{text}." if text else ""


def _normalise_summary(value: str, fallback: str = "") -> str:
    text = re.sub(r"\s+", " ", str(value or fallback or "")).strip()
    if PLACEHOLDER_SUMMARY_RE.search(text):
        text = re.sub(r"\s+", " ", str(fallback or "")).strip()
    if not text:
        return ""
    parts = re.findall(r"[^.!?。！？]+[.!?。！？]?", text)
    sentences = []
    for part in parts:
        sentence = part.strip()
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
            f"(Published: {_format_hkt(item.get('published_at', ''))}) URL: {item.get('url', 'N/A')}"
        )
    prompt = f"""Tech Brief: summarize semiconductor and technology supply chain headlines from the past {window_hours} hours.

RULES:
1. Return ONLY valid JSON: {{"items":[{{"rank":1,"key_sentence":"...","summary":"..."}}]}}.
2. Keep rank equal to the input rank. Do not invent links or sources.
3. key_sentence is one concise topic sentence, suitable to bold in Telegram. Do not include source/time/link text.
4. summary is exactly two short sentences where possible. Summarize the underlying news adequately: who/what happened, important numbers or counterparties, and why it matters for tech/semis/AI infrastructure.
5. Skip non-tech / low-signal headlines by omitting their rank.
6. Focus on semiconductors, foundries, memory, AI servers, substrates, PCB, CCL, IC design, GPUs, smartphones, PCs, data center power/cooling, AI infrastructure.
7. Do not do a full investment analysis here. This is a lightweight brief only.
8. Output every key_sentence and summary in English. Translate Chinese, Japanese, Korean, or other non-English source headlines into fluent English. Do not output any Chinese/Japanese/Korean characters.

Headlines:
{chr(10).join(lines)}
"""
    try:
        from anthropic import Anthropic

        client = Anthropic(timeout=90.0, max_retries=2)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        payload = _extract_json_object(resp.content[0].text)
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
- Do not output Chinese/Japanese/Korean characters.
- Do not add links, sources, or full investment analysis.
- The lookback window is {window_hours} hours.

Rows:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""
    from anthropic import Anthropic

    client = Anthropic(timeout=60.0, max_retries=1)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )
    result = _extract_json_object(resp.content[0].text)
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
    window_hours: int = 6,
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

    all_items = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_fetch_google_news, term, allowed_sources, 8, window_hours): term
            for term in terms
        }
        for fut in as_completed(futures):
            try:
                all_items.extend(fut.result())
            except Exception:
                pass

    unique = {}
    for item in all_items:
        if not _within_window(item.get("published_at", ""), window_hours, now_utc):
            continue
        key = _headline_key(item)
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
        [item for item in scored if item["score"] > 0],
        key=lambda x: (x["score"], _sort_timestamp(x)),
        reverse=True,
    )
    digest_items = []
    for idx, item in enumerate(ranked[:max_digest_items], start=1):
        resolved_url = _resolve_source_url(item.get("url", ""))
        ranked_item = {**item, "url": resolved_url or item.get("url", ""), "rank": idx}
        digest_items.append(ranked_item)
        stored_items[item["key"]] = ranked_item
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
    }
