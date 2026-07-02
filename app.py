"""Flask web GUI for the Research Pipeline."""

import truststore; truststore.inject_into_ssl()  # Use Windows cert store (handles Norton SSL inspection)

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import markdown
from flask import Flask, render_template, request, jsonify

from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_PATH = PROJECT_ROOT / "config" / "companies.json"
MACRO_DIR = DATA_DIR / "Macro"
THEMATIC_DIR = DATA_DIR / "Thematic"

MD_EXTENSIONS = ["tables", "fenced_code", "nl2br"]
DASHBOARD_CONFIG_PATH = PROJECT_ROOT / "config" / "dashboard.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_companies():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_json(path):
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (UnicodeDecodeError, json.JSONDecodeError):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    return json.load(f)
            except Exception:
                return None
    return None


def load_dashboard_config():
    return load_json(DASHBOARD_CONFIG_PATH) or {
        "news_topics": [], "portfolio_positions": [],
        "macro_events": [], "company_events": [],
    }


def render_md(text):
    """Render markdown string to HTML."""
    if not text:
        return ""
    return markdown.markdown(text, extensions=MD_EXTENSIONS)


def _fetch_forexfactory_red_events():
    """Fetch high-impact (red) events from ForexFactory for 4 weeks.
    Uses the free JSON API for this week, and Playwright scraping for future weeks."""
    import requests as http_req
    events = {}

    ff_date_set_api = set()

    # This week via free API (fast, reliable)
    try:
        resp = http_req.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            timeout=8, headers={"User-Agent": "Mozilla/5.0"},
        )
        if resp.status_code == 200:
            hkt = ZoneInfo("Asia/Hong_Kong")
            for evt in resp.json():
                if evt.get("impact", "").lower() != "high":
                    continue
                raw_date = evt.get("date", "")
                if not raw_date:
                    continue
                # Convert to HKT date — FF times are US Eastern
                try:
                    dt = datetime.fromisoformat(raw_date)
                    d = dt.astimezone(hkt).date().isoformat()
                except Exception:
                    d = raw_date[:10] if raw_date else ""
                if not d:
                    continue
                country = evt.get("country", "")
                title = evt.get("title", "")
                forecast = evt.get("forecast", "")
                previous = evt.get("previous", "")
                detail = f"{country} {title}"
                if forecast:
                    detail += f" (F: {forecast}"
                    if previous:
                        detail += f", P: {previous}"
                    detail += ")"
                events.setdefault(d, []).append({
                    "name": detail, "type": "macro", "region": country,
                })
                ff_date_set_api.add(d)
    except Exception:
        pass

    # Future weeks: use cache only (Playwright scraping runs offline via refresh_ff_calendar.py)
    ff_cache_path = PROJECT_ROOT / "data" / "Macro" / "_ff_calendar_cache.json"
    if ff_cache_path.exists():
        try:
            with open(ff_cache_path, encoding="utf-8") as f:
                cached = json.load(f)
            for d, evts in cached.items():
                events.setdefault(d, []).extend(evts)
        except Exception:
            pass

    return events

    # --- Below: Playwright scraping (disabled on page load, run via script) ---
    try:
        config = load_dashboard_config()
        tz = ZoneInfo(config.get("timezone", "Asia/Hong_Kong"))
        today = datetime.now(tz).date()
        # Next 3 Mondays
        next_monday = today + timedelta(days=(7 - today.weekday()))
        week_dates = []
        for i in range(3):
            monday = next_monday + timedelta(weeks=i)
            week_dates.append(monday.strftime("%b%d.%Y").lower())

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                ignore_https_errors=True,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            )
            for week_str in week_dates:
                page = ctx.new_page()
                try:
                    page.goto(
                        f"https://www.forexfactory.com/calendar?week={week_str}",
                        timeout=45000,
                    )
                    import time as _t
                    _t.sleep(6)

                    result = page.evaluate("""() => {
                        const rows = document.querySelectorAll('tr.calendar__row');
                        const events = [];
                        let currentDate = '';
                        for (const row of rows) {
                            const dateEl = row.querySelector('td.calendar__date span');
                            if (dateEl) currentDate = dateEl.textContent.trim();
                            const impactEl = row.querySelector('td.calendar__impact span');
                            if (!impactEl) continue;
                            if (!impactEl.className.includes('icon--ff-impact-red')) continue;
                            const titleEl = row.querySelector('td.calendar__event span');
                            const currEl = row.querySelector('td.calendar__currency');
                            events.push({
                                date: currentDate,
                                currency: currEl ? currEl.textContent.trim() : '',
                                title: titleEl ? titleEl.textContent.trim() : ''
                            });
                        }
                        return events;
                    }""")

                    # Parse dates: "Tue Mar 24" → "2026-03-24"
                    for evt in result:
                        date_text = evt.get("date", "")
                        try:
                            # Parse "Tue\nMar 24" or "Tue Mar 24"
                            parts = date_text.replace("\n", " ").strip().split()
                            if len(parts) >= 3:
                                month_str = parts[-2]
                                day_str = parts[-1]
                                dt = datetime.strptime(f"{month_str} {day_str} {today.year}", "%b %d %Y")
                                d = dt.strftime("%Y-%m-%d")
                            else:
                                continue
                        except Exception:
                            continue

                        detail = f"{evt['currency']} {evt['title']}"
                        events.setdefault(d, []).append({
                            "name": detail, "type": "macro", "region": evt["currency"],
                        })
                except Exception:
                    pass
                finally:
                    page.close()
                    _t.sleep(2)

            browser.close()

        # Cache the scraped future-week events
        future_events = {d: evts for d, evts in events.items()
                         if d not in ff_date_set_api}
        if future_events:
            ff_cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(ff_cache_path, "w", encoding="utf-8") as f:
                json.dump(future_events, f, ensure_ascii=False)
    except Exception:
        pass

    return events


def build_calendar_data():
    """Build 8 weeks of calendar data (this week + next 7 weeks) in HKT.
    Macro events come from ForexFactory red events. Company events from config/cache."""
    config = load_dashboard_config()
    tz_name = config.get("timezone", "Asia/Hong_Kong")
    tz = ZoneInfo(tz_name)
    today = datetime.now(tz).date()

    start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=55)  # 8 weeks

    events_by_date = {}

    # Macro events: ForexFactory for current week, manual config for future weeks
    ff_events = _fetch_forexfactory_red_events()
    for d, evts in ff_events.items():
        try:
            evt_date = date.fromisoformat(d)
        except ValueError:
            continue
        if start <= evt_date <= end:
            events_by_date.setdefault(d, []).extend(evts)

    # If FF scraping returned no future-week data, fall back to manual config
    ff_date_set = set(ff_events.keys())
    future_start = today + timedelta(days=7)
    has_future = any(date.fromisoformat(d) >= future_start for d in ff_date_set if d)
    if not has_future:
        for evt in config.get("macro_events", []):
            for d in evt.get("dates", []):
                if d in ff_date_set:
                    continue
                try:
                    evt_date = date.fromisoformat(d)
                except ValueError:
                    continue
                if start <= evt_date <= end:
                    events_by_date.setdefault(d, []).append({
                        "name": evt["name"],
                        "type": "macro",
                        "region": evt.get("region", ""),
                    })

    # Auto-sourced events from events_calendar.py cache
    try:
        from scripts.events_calendar import get_events
        auto_events = get_events()
        for d, evts in auto_events.items():
            try:
                evt_date = date.fromisoformat(d)
            except ValueError:
                continue
            if start <= evt_date <= end:
                events_by_date.setdefault(d, []).extend(evts)
    except Exception:
        pass

    # Company events from config (manual) — these take priority, added last
    # Track manual event keys to de-duplicate against auto-sourced events
    import re as _re
    for evt in config.get("company_events", []):
        d = evt.get("date", "")
        try:
            evt_date = date.fromisoformat(d)
        except ValueError:
            continue

        etype = "earnings" if any(kw in evt["name"].lower() for kw in ["earnings", "results", "tanshin", "revenue"]) else "company"
        label = f"{evt['ticker']}: {evt['name']}"

        # Check for multi-day events: "Mar 16-19" or "Jun 8-12"
        span_match = _re.search(r'(\w+)\s+(\d+)-(\d+)', evt["name"])
        if span_match:
            start_day = int(span_match.group(2))
            end_day = int(span_match.group(3))
            for day_offset in range(end_day - start_day + 1):
                span_date = evt_date + timedelta(days=day_offset)
                if start <= span_date <= end:
                    sd = span_date.isoformat()
                    # Remove any auto-sourced event for the same ticker on this date
                    existing = events_by_date.get(sd, [])
                    ticker_key = evt.get("ticker", "")
                    events_by_date[sd] = [
                        e for e in existing
                        if e.get("ticker", "") != ticker_key
                    ]
                    events_by_date[sd].append({
                        "name": label, "type": etype,
                    })
        else:
            if start <= evt_date <= end:
                # Remove any auto-sourced event for the same ticker on this date
                existing = events_by_date.get(d, [])
                ticker_key = evt.get("ticker", "")
                events_by_date[d] = [
                    e for e in existing
                    if e.get("ticker", "") != ticker_key
                ]
                events_by_date[d].append({
                    "name": label, "type": etype,
                })

    # Build day-by-day grid
    days = []
    current = start
    while current <= end:
        d_str = current.isoformat()
        days.append({
            "date": current,
            "day": current.day,
            "weekday": current.strftime("%a"),
            "is_today": current == today,
            "is_weekend": current.weekday() >= 5,
            "events": events_by_date.get(d_str, []),
        })
        current += timedelta(days=1)

    return days


def list_themes():
    if not THEMATIC_DIR.exists():
        return []
    return sorted([
        d.name for d in THEMATIC_DIR.iterdir()
        if d.is_dir() and (d / "linked_tickers.json").exists()
    ])


def list_macro_authors():
    authors_dir = MACRO_DIR / "authors"
    if not authors_dir.exists():
        return []
    return sorted([
        d.name for d in authors_dir.iterdir()
        if d.is_dir() and (d / "notes").exists()
    ])


@app.context_processor
def inject_sidebar():
    """Inject sidebar data into every template."""
    companies = load_companies()
    # Pre-sort companies by name for sidebar display
    sorted_companies = dict(sorted(companies.items(), key=lambda x: x[1].get("name", "").lower()))
    return {
        "companies": sorted_companies,
        "themes": list_themes(),
        "macro_authors": list_macro_authors(),
    }


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    companies = load_companies()

    # Count research notes per company
    company_stats = {}
    total_research_notes = 0
    for ticker, info in companies.items():
        notes_dir = DATA_DIR / ticker / "research" / "notes"
        n = len(list(notes_dir.glob("*.json"))) if notes_dir.exists() else 0
        total_research_notes += n
        company_stats[ticker] = {
            "name": info.get("name", ""),
            "market": info.get("market", ""),
            "notes": n,
        }

    # Macro stats
    macro_authors_list = list_macro_authors()
    macro_notes = 0
    for author in macro_authors_list:
        notes_dir = MACRO_DIR / "authors" / author / "notes"
        macro_notes += len(list(notes_dir.glob("*.json"))) if notes_dir.exists() else 0

    themes_list = list_themes()

    stats = {
        "companies": len(companies),
        "research_notes": total_research_notes,
        "macro_notes": macro_notes,
        "themes": len(themes_list),
        "macro_authors": len(macro_authors_list),
    }

    # Macro summary HTML
    macro_md_path = MACRO_DIR / "macro_summary.md"
    macro_summary_html = ""
    if macro_md_path.exists():
        macro_summary_html = render_md(macro_md_path.read_text(encoding="utf-8"))

    # Theme list with details
    theme_list = []
    for theme in themes_list:
        config = load_json(THEMATIC_DIR / theme / "linked_tickers.json")
        notes_dir = THEMATIC_DIR / theme / "notes"
        n = len(list(notes_dir.glob("*.json"))) if notes_dir.exists() else 0
        theme_list.append({
            "name": theme,
            "description": config.get("description", "") if config else "",
            "sources": n,
        })

    # Calendar
    calendar_days = build_calendar_data()

    # Dashboard config (for news topics and portfolio)
    dash_config = load_dashboard_config()

    return render_template("dashboard.html",
        stats=stats,
        macro_summary_html=macro_summary_html,
        theme_list=theme_list,
        company_stats=company_stats,
        calendar_days=calendar_days,
        dash_config=dash_config,
    )


# ---------------------------------------------------------------------------
# Company page
# ---------------------------------------------------------------------------

@app.route("/company/<ticker>")
def company_page(ticker):
    companies = load_companies()
    company = companies.get(ticker)
    if not company:
        return f"Ticker '{ticker}' not found.", 404

    ticker_dir = DATA_DIR / ticker

    # Filings
    filings = []
    filings_index = load_json(ticker_dir / "filings" / "index.json")
    if filings_index:
        filings = filings_index.get("filings", [])

    # Research summary
    research_summary_html = ""
    summary_md = ticker_dir / "research" / "summary.md"
    if summary_md.exists():
        research_summary_html = render_md(summary_md.read_text(encoding="utf-8"))

    # Research notes
    research_notes = []
    notes_dir = ticker_dir / "research" / "notes"
    if notes_dir.exists():
        for nf in sorted(notes_dir.glob("*.json"), reverse=True):
            note = load_json(nf)
            if not note:
                continue
            meta = note.get("metadata", {})
            rt = note.get("rating_and_target", {}) or {}
            analysis_report = note.get("analysis_report", "")
            research_notes.append({
                "date": meta.get("date", ""),
                "source": meta.get("source", "Unknown"),
                "title": meta.get("title", nf.stem),
                "rating": rt.get("rating"),
                "target_price": rt.get("target_price"),
                "detailed_summary": note.get("detailed_summary", ""),
                "analysis_report": analysis_report,
                "analysis_report_html": render_md(analysis_report) if analysis_report else "",
            })

    # Classified files
    classified_files = []
    class_path = ticker_dir / "classifications.json"
    if class_path.exists():
        class_data = load_json(class_path)
        if class_data:
            classified_files = class_data.get("files", [])

    return render_template("company.html",
        ticker=ticker,
        company=company,
        filings=filings,
        research_summary_html=research_summary_html,
        research_notes=research_notes,
        classified_files=classified_files,
    )


# ---------------------------------------------------------------------------
# Macro pages (stubs for now — will build out after core works)
# ---------------------------------------------------------------------------

@app.route("/macro")
def macro_page():
    return "Macro page — coming soon", 200


@app.route("/macro/author/<author>")
def macro_author_page(author):
    return f"Macro author: {author} — coming soon", 200


# ---------------------------------------------------------------------------
# Thematic pages (stubs)
# ---------------------------------------------------------------------------

@app.route("/thematic")
def thematic_page():
    return "Thematic page — coming soon", 200


@app.route("/thematic/<theme>")
def theme_detail_page(theme):
    return f"Theme: {theme} — coming soon", 200


# ---------------------------------------------------------------------------
# Action pages (stubs)
# ---------------------------------------------------------------------------

@app.route("/download")
def download_page():
    return "Download Materials page — coming soon", 200


@app.route("/store")
def store_page():
    return "Store Research page — coming soon", 200


# ---------------------------------------------------------------------------
# File serving (for viewing downloaded PDFs)
# ---------------------------------------------------------------------------

@app.route("/file/<ticker>/<path:filepath>")
def serve_file(ticker, filepath):
    from flask import send_from_directory
    return send_from_directory(DATA_DIR / ticker, filepath)


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.route("/api/companies")
def api_companies():
    return jsonify(load_companies())


DEFAULT_SOURCES = [
    "bloomberg", "wsj", "wall street journal", "ft", "financial times",
    "politico", "axios", "nyt", "new york times", "ny post", "nypost",
    "the economist", "economist", "business times", "reuters",
    "deltaone", "financialjuice", "walter bloomberg",
    "barron",
]

TECH_SOURCES = [
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

# Combined list — used by both the brief Tech section and the Portfolio firehose
ALL_APPROVED_SOURCES = DEFAULT_SOURCES + TECH_SOURCES


def _load_source_list(feed_name):
    """Load the source list for a specific feed from config, or use defaults."""
    config = load_dashboard_config()
    feed_sources = config.get("feed_sources", {})
    if feed_name in feed_sources:
        return [s.lower() for s in feed_sources[feed_name]]
    if feed_name in ("portfolio", "tech"):
        return [s.lower() for s in ALL_APPROVED_SOURCES]
    return [s.lower() for s in ALL_APPROVED_SOURCES]


def _is_approved_source(source_name, source_list=None):
    """Check if a source matches the approved list."""
    if not source_name:
        return False
    if source_list is None:
        source_list = DEFAULT_SOURCES
    s = source_name.lower().strip()
    for approved in source_list:
        if approved in s:
            return True
    return False


def _parse_rss_date(pub_date_str):
    """Parse RSS pubDate to HKT timestamp string."""
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


# Chinese equivalents for tech search terms — used for zh-TW Google News queries
ZH_SEARCH_TERMS = {
    "DRAM": "DRAM 記憶體",
    "NAND": "NAND 快閃記憶體",
    "memory": "記憶體",
    "AI servers": "AI伺服器",
    "GPU": "GPU 顯示卡",
    "CPU": "CPU 處理器",
    "foundry": "晶圓代工",
    "foundries": "晶圓代工",
    "substrate": "ABF載板",
    "substrates": "ABF載板 BT載板",
    "CCL": "銅箔基板 CCL",
    "PCB": "PCB 印刷電路板",
    "analog": "類比IC",
    "IC design": "IC設計",
    "smartphone": "智慧型手機",
    "semiconduct": "半導體",
    "probe card": "探針卡",
    "HBM": "HBM 高頻寬記憶體",
    "high bandwidth memory": "HBM 高頻寬記憶體",
}


def _fetch_google_news_rss(query, max_items=30, source_list=None, include_chinese=False, when="1d"):
    """Fetch headlines from Google News RSS, filtered by source list.
    If include_chinese=True, also queries zh-TW Google News for Chinese-language sources.
    `when` controls the time window (e.g. '1d', '3d', '7d')."""
    import requests as http_req
    import xml.etree.ElementTree as ET

    if source_list is None:
        source_list = DEFAULT_SOURCES

    items = []

    # Build list of (url, lang) pairs to fetch
    feeds = [
        (f"https://news.google.com/rss/search?q={query}+when:{when}&hl=en-US&gl=US&ceid=US:en", "en"),
    ]
    if include_chinese:
        # Also search zh-TW feed with original query
        feeds.append(
            (f"https://news.google.com/rss/search?q={query}+when:{when}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant", "zh")
        )
        # If query has a Chinese equivalent, search that too
        q_lower = query.lower().replace("+", " ")
        for eng_key, zh_term in ZH_SEARCH_TERMS.items():
            if eng_key.lower() in q_lower:
                zh_query = zh_term.replace(" ", "+")
                feeds.append(
                    (f"https://news.google.com/rss/search?q={zh_query}+when:{when}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant", "zh")
                )
                break

    seen_titles = set()
    for feed_url, lang in feeds:
        try:
            resp = http_req.get(feed_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                continue

            root = ET.fromstring(resp.content)
            for item_el in root.findall(".//item"):
                title = item_el.findtext("title", "")
                pub_date = item_el.findtext("pubDate", "")
                source = item_el.findtext("source", "")
                link = item_el.findtext("link", "")

                if " - " in title:
                    parts = title.rsplit(" - ", 1)
                    title = parts[0]
                    if not source:
                        source = parts[1]

                if not _is_approved_source(source, source_list):
                    continue

                # Deduplicate across feeds
                title_key = title[:40].lower()
                if title_key in seen_titles:
                    continue
                seen_titles.add(title_key)

                items.append({
                    "title": title,
                    "source": source,
                    "time": _parse_rss_date(pub_date),
                    "link": link.replace("/rss/articles/", "/articles/") if link else link,
                    "lang": lang,
                })

                if len(items) >= max_items:
                    return items
        except Exception as e:
            print(f"[news rss] fetch failed for {feed_url[:80]}... : {type(e).__name__}: {e}")
            continue

    return items


def _needs_translation(text):
    """Check if text contains significant non-ASCII (CJK) characters."""
    if not text:
        return False
    non_ascii = sum(1 for c in text if ord(c) > 127)
    return non_ascii > len(text) * 0.2


def _translate_non_english(items):
    """Translate non-English headlines to English via Claude API in a single batch."""
    to_translate = [(i, idx) for idx, i in enumerate(items) if _needs_translation(i.get("title", ""))]
    if not to_translate:
        return items

    try:
        from anthropic import Anthropic
        client = Anthropic(max_retries=2, timeout=30.0)

        # Build a numbered list for batch translation
        lines = []
        for item, idx in to_translate:
            lines.append(f"{idx}: {item['title']}")

        prompt = (
            "Translate each of the following news headlines into English. "
            "Preserve company names, ticker symbols, and technical terms. "
            "Return ONLY the translations, one per line, prefixed with the same number. "
            "Keep the translations concise and natural — these are financial news headlines.\n\n"
            + "\n".join(lines)
        )

        response = client.messages.create(
            # Headline translation is a constrained task — Haiku handles it at
            # a third of the cost; the previous Sonnet 4 ID was deprecated.
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        result = response.content[0].text

        # Parse translations back
        for line in result.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            # Parse "idx: translated text"
            colon_pos = line.find(":")
            if colon_pos == -1:
                continue
            try:
                idx = int(line[:colon_pos].strip())
                translated = line[colon_pos + 1:].strip()
                if 0 <= idx < len(items) and translated:
                    original = items[idx]["title"]
                    items[idx]["title"] = translated
                    items[idx]["original_title"] = original
            except (ValueError, IndexError):
                continue

    except Exception:
        pass  # If translation fails, keep originals

    return items


@app.route("/api/news/topics")
def api_news_topics():
    """Fetch recent headlines for configured key topics (parallel)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    config = load_dashboard_config()
    topics = config.get("news_topics", [])
    if not topics:
        return jsonify({"items": []})

    source_list = _load_source_list("topics")

    def _fetch_topic(topic):
        query = topic.replace(" ", "+")
        items = _fetch_google_news_rss(query, max_items=10, source_list=source_list, include_chinese=True)
        for item in items:
            item["topic"] = topic
        return items

    all_items = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_topic, t): t for t in topics}
        for future in as_completed(futures):
            try:
                all_items.extend(future.result())
            except Exception:
                pass

    # Deduplicate
    seen_titles = set()
    unique = []
    for item in all_items:
        title_key = item["title"][:60].lower()
        if title_key not in seen_titles:
            seen_titles.add(title_key)
            unique.append(item)

    unique.sort(key=lambda x: x.get("time", ""), reverse=True)
    unique = _translate_non_english(unique[:30])
    return jsonify({"items": unique})


@app.route("/api/news/portfolio")
def api_news_portfolio():
    """Fetch recent headlines for portfolio positions (parallel)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    config = load_dashboard_config()
    companies_data = load_companies()
    positions = config.get("portfolio_positions", [])
    if not positions:
        return jsonify({"items": []})

    source_list = _load_source_list("portfolio")

    def _fetch_position(ticker):
        company_name = companies_data.get(ticker, {}).get("name", ticker)
        query = company_name.replace(" ", "+")
        market = companies_data.get(ticker, {}).get("market", "US")
        use_zh = market in ("TW", "JP", "KR", "CN")
        items = _fetch_google_news_rss(query, max_items=8, source_list=source_list, include_chinese=use_zh)
        for item in items:
            item["source"] = f"{ticker} · {item['source']}" if item.get("source") else ticker
        return items

    all_items = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_position, t): t for t in positions}
        for future in as_completed(futures):
            try:
                all_items.extend(future.result())
            except Exception:
                pass

    # Deduplicate
    seen_titles = set()
    unique = []
    for item in all_items:
        title_key = item["title"][:60].lower()
        if title_key not in seen_titles:
            seen_titles.add(title_key)
            unique.append(item)

    unique.sort(key=lambda x: x.get("time", ""), reverse=True)
    unique = _translate_non_english(unique[:30])
    return jsonify({"items": unique})


@app.route("/api/news/company/<ticker>")
def api_news_company(ticker):
    """Fetch recent headlines for a single company (72-hour window)."""
    companies_data = load_companies()
    company = companies_data.get(ticker)
    if not company:
        return jsonify({"items": []})

    company_name = company.get("name", ticker)
    market = company.get("market", "US")
    use_zh = market in ("TW", "JP", "KR", "CN")
    source_list = ALL_APPROVED_SOURCES

    items = _fetch_google_news_rss(
        company_name.replace(" ", "+"),
        max_items=30,
        source_list=source_list,
        include_chinese=use_zh,
        when="3d",
    )

    # Also search by ticker symbol for broader coverage
    ticker_query = ticker.replace(" ", "+")
    if ticker_query != company_name.replace(" ", "+"):
        ticker_items = _fetch_google_news_rss(
            ticker_query,
            max_items=15,
            source_list=source_list,
            include_chinese=use_zh,
            when="3d",
        )
        # Deduplicate against existing
        seen = {item["title"][:40].lower() for item in items}
        for item in ticker_items:
            if item["title"][:40].lower() not in seen:
                seen.add(item["title"][:40].lower())
                items.append(item)

    items.sort(key=lambda x: x.get("time", ""), reverse=True)
    items = _translate_non_english(items[:30])
    return jsonify({"items": items})


@app.route("/api/brief")
def api_brief():
    """Generate Morning or Evening Brief via subprocess to avoid connection issues."""
    import subprocess, os
    import sys as _sys

    # Check for cached brief (regenerate at most once per hour, unless force=1)
    force = request.args.get("force") == "1"
    brief_cache = PROJECT_ROOT / "data" / "Macro" / "_brief_cache.json"
    if not force and brief_cache.exists():
        try:
            cache_age = datetime.now().timestamp() - brief_cache.stat().st_mtime
            if cache_age < 3600:
                with open(brief_cache, encoding="utf-8") as f:
                    return jsonify(json.load(f))
        except Exception:
            pass

    try:
        python_exe = _sys.executable
        result = subprocess.run(
            [python_exe, str(PROJECT_ROOT / "generate_brief.py")],
            capture_output=True, text=True, timeout=180,
            cwd=str(PROJECT_ROOT),
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout.strip())
            # Cache the result
            brief_cache.parent.mkdir(parents=True, exist_ok=True)
            with open(brief_cache, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            return jsonify(data)
        else:
            err = (result.stderr or "Unknown error")[:300]
            return jsonify({"type": "Brief", "html": f"<p>Brief generation failed: {err}</p>", "error": err})
    except subprocess.TimeoutExpired:
        return jsonify({"type": "Brief", "html": "<p>Brief generation timed out (180s).</p>", "error": "timeout"})
    except Exception as e:
        return jsonify({"type": "Brief", "html": f"<p>Error: {str(e)[:100]}</p>", "error": str(e)})


@app.route("/api/tech-brief")
def api_tech_brief():
    """Generate Tech Brief via subprocess."""
    import subprocess, os
    import sys as _sys

    force = request.args.get("force") == "1"
    tech_cache = PROJECT_ROOT / "data" / "Macro" / "_tech_brief_cache.json"
    if not force and tech_cache.exists():
        try:
            cache_age = datetime.now().timestamp() - tech_cache.stat().st_mtime
            if cache_age < 3600:
                with open(tech_cache, encoding="utf-8") as f:
                    return jsonify(json.load(f))
        except Exception:
            pass

    try:
        result = subprocess.run(
            [_sys.executable, str(PROJECT_ROOT / "generate_tech_brief.py")],
            capture_output=True, text=True, timeout=180,
            cwd=str(PROJECT_ROOT),
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout.strip())
            tech_cache.parent.mkdir(parents=True, exist_ok=True)
            with open(tech_cache, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            return jsonify(data)
        else:
            err = (result.stderr or "Unknown error")[:300]
            return jsonify({"html": f"<p>Tech brief failed: {err}</p>", "error": err})
    except subprocess.TimeoutExpired:
        return jsonify({"html": "<p>Tech brief timed out.</p>", "error": "timeout"})
    except Exception as e:
        return jsonify({"html": f"<p>Error: {str(e)[:100]}</p>", "error": str(e)})


@app.route("/api/forexfactory-calendar")
def api_forexfactory_calendar():
    """Fetch high-impact (red) events from ForexFactory."""
    import requests as http_req

    try:
        # ForexFactory provides a calendar XML/RSS feed
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        resp = http_req.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return jsonify({"events": []})

        events = []
        for evt in resp.json():
            if evt.get("impact", "").lower() == "high":
                events.append({
                    "name": f"{evt.get('country', '')} {evt.get('title', '')}",
                    "date": evt.get("date", ""),
                    "time": evt.get("time", ""),
                    "forecast": evt.get("forecast", ""),
                    "previous": evt.get("previous", ""),
                })
        return jsonify({"events": events})
    except Exception:
        return jsonify({"events": []})


@app.route("/api/feed-sources/<feed_name>", methods=["GET", "POST"])
def api_feed_sources(feed_name):
    """Get or set the source list for a specific news feed."""
    config = load_dashboard_config()
    if request.method == "POST":
        data = request.get_json()
        sources = data.get("sources", [])
        config.setdefault("feed_sources", {})[feed_name] = sources
        with open(DASHBOARD_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
            f.write("\n")
        return jsonify({"status": "ok"})

    # GET — return current sources for this feed
    sources = _load_source_list(feed_name)
    return jsonify({"feed": feed_name, "sources": sources})


@app.route("/api/dashboard-config", methods=["GET", "POST"])
def api_dashboard_config():
    if request.method == "POST":
        data = request.get_json()
        with open(DASHBOARD_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        return jsonify({"status": "ok"})
    return jsonify(load_dashboard_config())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import socket
    # Find a free port to avoid zombie process conflicts
    for port in range(5080, 5100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                break
    print(f"Starting on http://localhost:{port}")
    app.run(debug=False, port=port, threaded=True)
