"""Standalone script to generate the Tech Brief — last 24h of semiconductor/tech headlines."""
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

APPROVED_SOURCES = [
    "bloomberg", "wsj", "wall street journal", "ft", "financial times",
    "reuters", "nyt", "new york times", "the economist", "business times",
    "nikkei", "scmp", "south china morning post",
    "commercial times", "ctee.com", "ctee",
    "digitimes", "udn", "money.udn", "trendforce",
    "counterpointresearch", "counterpoint", "gartner",
    "anue", "cnyes", "ltn", "ec.ltn",
    "chinatimes", "china times", "technews", "finance.technews",
    "ijiwei", "semianalysis",
]

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
    # Chinese keywords
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


def load_dashboard_config():
    with open(DASHBOARD_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


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


def _fetch_rss(query, max_items=5):
    items = []
    seen = set()
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
                if not any(a in s for a in APPROVED_SOURCES):
                    continue
                key = title[:40].lower()
                if key in seen:
                    continue
                seen.add(key)
                real_link = link.replace("/rss/articles/", "/articles/") if link else link
                items.append({"title": title, "source": source, "time": _parse_rss_date(pub_date), "link": real_link})
                if len(items) >= max_items:
                    return items
        except Exception:
            continue
    return items


def main():
    all_headlines = []

    def _fetch(term):
        return _fetch_rss(term.replace(" ", "+"), max_items=5)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch, t): t for t in TECH_SEARCH_TERMS}
        for f in as_completed(futures):
            try:
                all_headlines.extend(f.result())
            except Exception:
                pass

    # Deduplicate
    seen = set()
    unique = []
    for h in all_headlines:
        key = h["title"][:40].lower()
        if key not in seen:
            seen.add(key)
            unique.append(h)

    max_headlines = min(len(unique), 30)
    headlines_text = "\n".join(
        f"- [{h['source']}] {h['title']} (Published: {h['time']}) URL: {h.get('link', 'N/A')}"
        for h in unique[:max_headlines]
    )

    if not headlines_text.strip():
        print(json.dumps({"html": "<p>No tech headlines in the past 24 hours.</p>", "markdown": ""}))
        return

    import time as _time
    from anthropic import Anthropic

    prompt = f"""Tech Brief: summarize semiconductor and technology supply chain headlines from the past 24 hours.

RULES:
1. Each headline gets its own bullet with 2-3 sentence blurb. Bold first sentence.
2. End each bullet with source as markdown link: ([Digitimes, 16 March 08:00 HKT](URL)).
3. No horizontal rules. No section headers needed — just bullets.
4. Skip non-tech headlines. Focus on: semiconductors, foundries, memory, AI servers, substrates, PCB, CCL, IC design, GPUs, smartphones, PCs.
5. Be specific with names, numbers, prices, companies.
6. Skip sports, entertainment, lifestyle content.

Headlines:
{headlines_text}"""

    client = Anthropic(timeout=90.0)
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=3000,
                messages=[{"role": "user", "content": prompt}],
            )
            break
        except Exception:
            if attempt < 2:
                _time.sleep(5 * (attempt + 1))
                client = Anthropic(timeout=90.0)
                continue
            raise

    brief_md = resp.content[0].text

    # Normalize bullets
    lines = []
    for line in brief_md.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("•") or stripped.startswith("�"):
            stripped = "- " + stripped[1:].strip()
        elif stripped.startswith("**") and not stripped.startswith("- "):
            stripped = "- " + stripped
        # Skip LLM meta-commentary
        ll = stripped.lower()
        if ll.startswith("based on") or "no briefs" in ll or "none qualify" in ll:
            continue
        if ll.startswith("# "):
            continue
        lines.append(stripped)

    brief_md = "\n\n".join(lines)
    brief_html = markdown.markdown(brief_md, extensions=["tables", "fenced_code", "nl2br"])

    print(json.dumps({"html": brief_html, "markdown": brief_md}))


if __name__ == "__main__":
    main()
