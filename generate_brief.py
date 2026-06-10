"""Standalone script to generate the Morning/Evening Brief.
Called as a subprocess from the Flask app. No Flask imports."""
import truststore; truststore.inject_into_ssl()  # Use Windows cert store (handles Norton SSL inspection)

import json
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import markdown
import requests as http_requests

from dotenv import load_dotenv
load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent
DASHBOARD_CONFIG_PATH = PROJECT_ROOT / "config" / "dashboard.json"

DEFAULT_SOURCES = [
    "bloomberg", "wsj", "wall street journal", "ft", "financial times",
    "politico", "axios", "nyt", "new york times", "ny post", "nypost",
    "the economist", "economist", "business times", "reuters",
    "deltaone", "financialjuice", "walter bloomberg",
    # Tech/Asia sources (user-specified)
    "commercial times", "ctee.com", "ctee",
    "digitimes",
    "udn", "money.udn",
    "trendforce",
    "counterpointresearch", "counterpoint",
    "gartner",
    "anue", "cnyes",
    "ltn", "ec.ltn",
    "chinatimes", "china times",
    "technews", "finance.technews",
    "ijiwei",
    "semianalysis",
    "nikkei",
]


def load_dashboard_config():
    with open(DASHBOARD_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def render_md(text):
    return markdown.markdown(text or "", extensions=["tables", "fenced_code", "nl2br"])


def _parse_rss_date(pub_date_str):
    if not pub_date_str:
        return ""
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(pub_date_str)
        config = load_dashboard_config()
        tz = ZoneInfo(config.get("timezone", "Asia/Hong_Kong"))
        dt_local = dt.astimezone(tz)
        day = dt_local.day
        return f"{day} {dt_local.strftime('%B')}, {dt_local.strftime('%H:%M')} HKT"
    except Exception:
        return pub_date_str[:16]


# Direct RSS feeds from Chinese tech/finance sources (not dependent on Google News indexing)
DIRECT_RSS_FEEDS = {
    "tech": [
        "https://www.digitimes.com/rss/daily_news_rss.xml",
    ],
}


def _fetch_direct_rss(feed_url, max_items=5):
    """Fetch articles directly from a source's own RSS feed."""
    items = []
    try:
        resp = http_requests.get(feed_url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return items
        root = ET.fromstring(resp.content)
        for el in root.findall(".//item"):
            title = el.findtext("title", "")
            link = el.findtext("link", "")
            pub_date = el.findtext("pubDate", "")
            source = feed_url.split("/")[2].replace("www.", "")  # e.g. "digitimes.com"
            if " - " in title:
                parts = title.rsplit(" - ", 1)
                title = parts[0]
            items.append({
                "title": title, "source": source,
                "time": _parse_rss_date(pub_date), "link": link.replace("/rss/articles/", "/articles/") if link else link,
            })
            if len(items) >= max_items:
                break
    except Exception:
        pass
    return items


def _fetch_rss(query, max_items=5):
    items = []
    seen = set()
    # Search English, zh-TW, and zh-CN feeds
    feeds = [
        f"https://news.google.com/rss/search?q={query}+when:1d&hl=en-US&gl=US&ceid=US:en",
        f"https://news.google.com/rss/search?q={query}+when:1d&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        f"https://news.google.com/rss/search?q={query}+when:1d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
    ]
    for feed_url in feeds:
        try:
            resp = http_requests.get(feed_url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                continue
            root = ET.fromstring(resp.content)
            for el in root.findall(".//item"):
                title = el.findtext("title", "")
                pub_date = el.findtext("pubDate", "")
                source = el.findtext("source", "")
                link = el.findtext("link", "")
                if " - " in title:
                    parts = title.rsplit(" - ", 1)
                    title = parts[0]
                    if not source:
                        source = parts[1]
                s = (source or "").lower()
                if not any(a in s for a in DEFAULT_SOURCES):
                    continue
                key = title[:40].lower()
                if key in seen:
                    continue
                seen.add(key)
                items.append({
                    "title": title, "source": source,
                    "time": _parse_rss_date(pub_date), "link": link.replace("/rss/articles/", "/articles/") if link else link,
                })
                if len(items) >= max_items:
                    return items
        except Exception:
            continue
    return items


def main():
    config = load_dashboard_config()
    tz = ZoneInfo(config.get("timezone", "Asia/Hong_Kong"))
    now = datetime.now(tz)
    hour = now.hour

    if 8 <= hour < 20:
        brief_type = "Morning"
        window_end = now.replace(hour=8, minute=0, second=0, microsecond=0)
        window_start = window_end - timedelta(hours=12)
    else:
        brief_type = "Evening"
        window_end = now.replace(hour=20, minute=0, second=0, microsecond=0)
        if hour < 8:
            window_end = window_end - timedelta(days=1)
        window_start = window_end - timedelta(hours=12)

    # Collect search terms from brief sections
    brief_sections = config.get("brief_sections", [])
    search_terms = []
    for section in brief_sections:
        search_terms.extend(section.get("topics", []))
        search_terms.extend(section.get("subsections", []))
    search_terms = [t for t in search_terms if t]
    broad = ["financial markets today", "geopolitics"]

    # Add Chinese-keyword variants for tech/semiconductor topics to catch
    # Chinese-language sources (UDN, ChinaTimes, Anue, etc.)
    zh_tech_terms = [
        "半導體 晶圓代工",  # semiconductor foundry
        "ABF載板 CCL",      # ABF substrate, CCL
        "DRAM HBM 記憶體",  # memory
        "AI伺服器",          # AI server
        "台積電",            # TSMC
        "鴻海",              # Foxconn
        "聯電",              # UMC
        "GTC 輝達",          # GTC NVIDIA
        "IC設計 漲價",       # IC design price hike
    ]

    # Also add site-specific searches for key Chinese sources
    site_searches = [
        "site:money.udn.com 半導體",
        "site:chinatimes.com 半導體",
        "site:news.cnyes.com 半導體",
        "site:finance.technews.tw 半導體",
    ]

    all_terms = search_terms + broad + zh_tech_terms + site_searches

    # Fetch headlines in parallel
    all_headlines = []

    # Also fetch from direct RSS feeds
    for feed_url in DIRECT_RSS_FEEDS.get("tech", []):
        try:
            all_headlines.extend(_fetch_direct_rss(feed_url, max_items=5))
        except Exception:
            pass

    def _fetch_term(term):
        return _fetch_rss(term.replace(" ", "+"), max_items=5)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_term, t): t for t in all_terms}
        for future in as_completed(futures):
            try:
                all_headlines.extend(future.result())
            except Exception:
                pass

    # Deduplicate
    seen = set()
    unique = []
    for h in all_headlines:
        key = h["title"][:50].lower()
        if key not in seen:
            seen.add(key)
            unique.append(h)

    max_headlines = min(len(unique), 45)
    headlines_text = "\n".join(
        f"- [{h['source']}] {h['title']} (Published: {h['time']}) URL: {h.get('link', 'N/A')}"
        for h in unique[:max_headlines]
    )

    # Build structure text
    structure_text = ""
    for section in brief_sections:
        heading = section.get("heading", "")
        topics = section.get("topics", [])
        subs = section.get("subsections", [])
        structure_text += f"\n### {heading}\n"
        if topics:
            structure_text += f"  Topics: {', '.join(topics)}\n"
        if subs:
            for s in subs:
                structure_text += f"  - {s}\n"

    # Call Claude with retries
    import time as _time
    from anthropic import Anthropic
    client = Anthropic(timeout=180.0)

    # Split headlines into chunks and make parallel API calls for speed/reliability
    chunk_size = 8
    headline_list = unique[:max_headlines]
    chunks = [headline_list[i:i+chunk_size] for i in range(0, len(headline_list), chunk_size)]

    # Build list of valid section names (exclude Tech — it has its own brief)
    valid_sections = [s.get("heading", "") for s in brief_sections if s.get("heading", "") != "Tech"]
    valid_sections_str = ", ".join(valid_sections)

    base_rules = f"""{brief_type} Brief for hedge fund PM. Past 12h ({window_start.strftime('%b %d %H:%M')}-{window_end.strftime('%b %d %H:%M')} HKT).

RULES:
1. Each headline gets its OWN bullet with a 2-3 sentence blurb. Bold the first sentence (topic sentence). Remaining sentences explain context, details, significance.
2. Timestamps = article PUBLICATION time, not event time. Don't conflate.
3. End each bullet with source as markdown link: ([Reuters, 15 March 15:52 HKT](URL)).
4. No horizontal rules (---). Use ONLY these section headers: {valid_sections_str}. Do NOT invent new sections. If a headline doesn't fit any section, place it under the closest match or skip it.
5. Be specific: exact names, numbers, places, direct quotes.
6. Skip headlines about sports, entertainment, lifestyle, or anything not market/finance/geopolitics/tech/crypto related."""

    # Process chunks in parallel
    from concurrent.futures import as_completed as _as_completed

    # Static instructions go in a cached system block so parallel chunk calls
    # share one prompt-cache entry instead of resending the rules each time.
    system_block = [{
        "type": "text",
        "text": f"""{base_rules}

Sections:
{structure_text}

IMPORTANT: Route each headline to the CORRECT section. Tech headlines (semiconductors, AI servers, foundries, GPUs, substrates, PCB, memory) go under Tech. AI headlines (OpenAI, Anthropic, Gemini) go under AI. Crypto headlines go under Crypto. Asia-specific headlines go under the relevant country. Do NOT put everything under World.""",
        "cache_control": {"type": "ephemeral"},
    }]

    def _process_chunk(chunk_headlines, chunk_idx):
        chunk_text = "\n".join(
            f"- [{h['source']}] {h['title']} (Published: {h['time']}) URL: {h.get('link', 'N/A')}"
            for h in chunk_headlines
        )
        prompt = f"Headlines (batch {chunk_idx+1}):\n{chunk_text}"
        for attempt in range(3):
            try:
                resp = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=2000,
                    system=system_block,
                    messages=[{"role": "user", "content": prompt}],
                )
                return resp.content[0].text
            except Exception:
                if attempt < 2:
                    _time.sleep(3 * (attempt + 1))
                    continue
                return ""

    chunk_results = [""] * len(chunks)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_process_chunk, c, i): i for i, c in enumerate(chunks)}
        for future in _as_completed(futures):
            idx = futures[future]
            try:
                chunk_results[idx] = future.result()
            except Exception:
                pass

    # Merge chunk results — deduplicate section headers, filter invalid sections
    valid_headers_set = {s.lower() for s in valid_sections}
    merged_sections = {}
    current_section = ""

    for result in chunk_results:
        if not result.strip():
            continue
        # Strip LLM meta-commentary
        lines = []
        for line in result.split("\n"):
            ll = line.lower().strip()
            if ll.startswith("based on") or "no briefs to report" in ll or "none qualify" in ll:
                continue
            if ll.startswith("**result:**") or ll.startswith("> \"skip"):
                continue
            if ll.startswith("# ") and not ll.startswith("## "):
                continue  # skip H1 titles like "# Morning Brief"
            lines.append(line)

        for line in lines:
            # Match ## or ### headers
            if line.startswith("## ") or line.startswith("### "):
                header_text = line.lstrip("#").strip()
                # Fuzzy match to valid sections
                matched_section = ""
                for vs in valid_sections:
                    if vs.lower() in header_text.lower() or header_text.lower() in vs.lower():
                        matched_section = vs
                        break
                if matched_section:
                    current_section = f"## {matched_section}"
                    if current_section not in merged_sections:
                        merged_sections[current_section] = []
                else:
                    current_section = ""  # skip invalid
            elif line.strip() and current_section:
                merged_sections[current_section].append(line)

    # Output in the same order as brief_sections config, with normalized bullet formatting
    brief_md = ""
    for section_name in valid_sections:
        header = f"## {section_name}"
        if header in merged_sections and merged_sections[header]:
            normalized = []
            for line in merged_sections[header]:
                stripped = line.strip()
                if not stripped:
                    continue
                # Normalize bullets: convert •, *, or bare bold to "- "
                if stripped.startswith("•") or stripped.startswith("�"):
                    stripped = "- " + stripped[1:].strip()
                elif stripped.startswith("* **"):
                    stripped = "- **" + stripped[3:].strip()
                elif stripped.startswith("**") and not stripped.startswith("- "):
                    stripped = "- " + stripped
                normalized.append(stripped)
            if normalized:
                brief_md += f"{header}\n\n" + "\n\n".join(normalized) + "\n\n"
    brief_html = render_md(brief_md)
    print(json.dumps({"type": brief_type, "html": brief_html, "markdown": brief_md}))


if __name__ == "__main__":
    main()
