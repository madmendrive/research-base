"""Automated events calendar — fetches earnings dates, revenue reporting dates,
and corporate events from multiple sources across US, JP, TW, KR markets.

Sources:
  US:  NASDAQ earnings calendar API
  JP:  JPX scheduled earnings announcement Excel files
  TW:  Monthly revenue reporting dates (10th of each month) + MOPS
  KR:  DART API upcoming disclosures

Results are cached in data/Macro/_events_cache.json with a configurable TTL.
Manual events in config/dashboard.json take priority over auto-sourced ones.
"""

import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import requests as http_requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_PATH = PROJECT_ROOT / "config" / "companies.json"
CACHE_PATH = DATA_DIR / "Macro" / "_events_cache.json"

CACHE_TTL_HOURS = 12
REQUEST_DELAY = 0.5
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _load_companies():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _companies_by_market():
    companies = _load_companies()
    by_market = {}
    for ticker, info in companies.items():
        m = info.get("market", "other")
        by_market.setdefault(m, {})[ticker] = info
    return by_market


def _save_cache(events):
    """Save events dict to cache with timestamp."""
    cache = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "events": events,
    }
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _load_cache():
    """Load cached events if fresh enough. Returns (events_dict, is_fresh)."""
    if not CACHE_PATH.exists():
        return {}, False
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            cache = json.load(f)
        fetched_at = datetime.fromisoformat(cache["fetched_at"])
        age_hours = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 3600
        return cache.get("events", {}), age_hours < CACHE_TTL_HOURS
    except Exception:
        return {}, False


# ---------------------------------------------------------------------------
# US: NASDAQ earnings calendar API
# ---------------------------------------------------------------------------

def fetch_us_earnings(us_companies, days_ahead=60):
    """Fetch upcoming US earnings dates from NASDAQ API.
    Scans each day in the range and filters to our tickers."""
    our_tickers = set(us_companies.keys())
    events = {}

    today = datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
    end = today + timedelta(days=days_ahead)

    print(f"  US: scanning NASDAQ earnings API ({days_ahead} days)...")
    checked = 0
    found = 0

    current = today
    while current <= end:
        # Skip weekends
        if current.weekday() >= 5:
            current += timedelta(days=1)
            continue

        d_str = current.isoformat()
        try:
            resp = http_requests.get(
                "https://api.nasdaq.com/api/calendar/earnings",
                params={"date": d_str},
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=10,
            )
            if resp.status_code == 200:
                rows = resp.json().get("data", {}).get("rows", [])
                for row in rows:
                    symbol = row.get("symbol", "")
                    if symbol in our_tickers:
                        name = us_companies[symbol].get("name", symbol)
                        time_str = row.get("time", "")
                        timing = ""
                        if "pre-market" in time_str:
                            timing = " (BMO)"
                        elif "after-hours" in time_str:
                            timing = " (AMC)"
                        events.setdefault(d_str, []).append({
                            "name": f"{symbol}: Earnings{timing}",
                            "type": "earnings",
                            "ticker": symbol,
                            "region": "US",
                        })
                        found += 1
        except Exception:
            pass

        checked += 1
        current += timedelta(days=1)

        # Rate limit: small delay every 5 requests
        if checked % 5 == 0:
            time.sleep(REQUEST_DELAY)

    print(f"  US: found {found} earnings date(s) across {checked} business days")
    return events


# ---------------------------------------------------------------------------
# JP: JPX scheduled earnings announcements (Excel files)
# ---------------------------------------------------------------------------

def fetch_jp_earnings(jp_companies, days_ahead=90):
    """Fetch scheduled earnings dates from JPX Excel files.
    JPX publishes one Excel per fiscal quarter-end month."""
    events = {}

    # Our JP stock codes (4-digit)
    our_codes = {}
    for ticker, info in jp_companies.items():
        code = ticker.split()[0]
        our_codes[code] = (ticker, info.get("name", ticker))

    print(f"  JP: fetching JPX earnings schedule...")

    # JPX page lists Excel links — scrape the page to find current files
    try:
        resp = http_requests.get(
            "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html",
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        excel_links = re.findall(
            r'href="([^"]*(?:kessan\d+[^"]*\.xlsx))"', resp.text
        )
    except Exception:
        excel_links = []

    if not excel_links:
        print("  JP: no Excel files found on JPX page")
        return events

    print(f"  JP: found {len(excel_links)} Excel file(s)")

    today = datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
    end = today + timedelta(days=days_ahead)
    found = 0

    for link in excel_links:
        if not link.startswith("http"):
            link = "https://www.jpx.co.jp" + link

        try:
            resp = http_requests.get(
                link, headers={"User-Agent": USER_AGENT}, timeout=15
            )
            if resp.status_code != 200:
                continue

            import io
            import pandas as pd

            df = pd.read_excel(io.BytesIO(resp.content), header=None)

            # Data starts at row 4: col 0=date, col 1=code, col 3=name(EN), col 8=period
            for idx in range(4, len(df)):
                row = df.iloc[idx]
                code = str(row[1]).strip() if pd.notna(row[1]) else ""
                if code not in our_codes:
                    continue

                ann_date = row[0]
                if pd.isna(ann_date):
                    continue

                # Parse the date
                if isinstance(ann_date, datetime):
                    d = ann_date.date()
                elif isinstance(ann_date, date):
                    d = ann_date
                else:
                    try:
                        d = datetime.strptime(str(ann_date)[:10], "%Y-%m-%d").date()
                    except Exception:
                        continue

                if d < today or d > end:
                    continue

                ticker, name = our_codes[code]
                period = str(row[8]) if pd.notna(row[8]) else ""
                period_label = ""
                if "first quarter" in period.lower() or "第１四半期" in str(row[7]):
                    period_label = "Q1"
                elif "second quarter" in period.lower() or "半期" in str(row[7]):
                    period_label = "H1"
                elif "third quarter" in period.lower() or "第３四半期" in str(row[7]):
                    period_label = "Q3"
                elif "full" in period.lower() or "通期" in str(row[7]) or "本決算" in str(row[7]):
                    period_label = "FY"

                label = f"{ticker}: Earnings"
                if period_label:
                    label += f" ({period_label})"

                d_str = d.isoformat()
                events.setdefault(d_str, []).append({
                    "name": label,
                    "type": "earnings",
                    "ticker": ticker,
                    "region": "JP",
                })
                found += 1

            time.sleep(REQUEST_DELAY)
        except Exception as e:
            print(f"  JP: error reading {link}: {str(e)[:60]}")

    print(f"  JP: found {found} earnings date(s)")
    return events


# ---------------------------------------------------------------------------
# TW: Monthly revenue reporting dates + earnings
# ---------------------------------------------------------------------------

def fetch_tw_events(tw_companies, days_ahead=60):
    """Generate Taiwan company events:
    1. Monthly revenue reports — Taiwan-listed companies must report monthly
       revenue by the 10th of the following month.
    2. Quarterly earnings — typically reported within specific windows.
    """
    events = {}
    today = datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
    end = today + timedelta(days=days_ahead)
    found = 0

    print(f"  TW: generating monthly revenue reporting dates...")

    # Monthly revenue: due by 10th of following month
    # If the 10th falls on a weekend, shift to the next business day (Monday)
    current_month = today.replace(day=1)
    tw_tickers_sorted = sorted(tw_companies.keys())

    for _ in range(4):  # up to 4 months ahead
        # Revenue for the previous month is due by the 10th
        revenue_deadline = current_month.replace(day=10)
        # Shift weekends to Monday
        if revenue_deadline.weekday() == 5:  # Saturday
            revenue_deadline += timedelta(days=2)
        elif revenue_deadline.weekday() == 6:  # Sunday
            revenue_deadline += timedelta(days=1)

        revenue_month = current_month - timedelta(days=1)  # previous month

        if today <= revenue_deadline <= end:
            d_str = revenue_deadline.isoformat()
            month_name = revenue_month.strftime("%B %Y")
            ticker_list = ", ".join(tw_tickers_sorted)

            events.setdefault(d_str, []).append({
                "name": f"{month_name} Revenue Report: {ticker_list}",
                "type": "revenue",
                "ticker": "TW-ALL",
                "region": "TW",
            })
            found += 1

        current_month = (current_month + timedelta(days=32)).replace(day=1)

    # Quarterly earnings windows (approximate):
    # Q1 (Jan-Mar): reported late April / early May
    # Q2 (Apr-Jun): reported mid-August
    # Q3 (Jul-Sep): reported mid-November
    # Q4 / FY (Oct-Dec): reported late March
    # These are approximate — TWSE requires within 45 days of quarter end
    earnings_windows = [
        # (quarter, approximate report month, approximate day)
        ("Q4/FY", 3, 31),   # March 31 for full year
        ("Q1", 5, 15),      # May 15 for Q1
        ("Q2", 8, 14),      # Aug 14 for Q2
        ("Q3", 11, 14),     # Nov 14 for Q3
    ]

    for quarter, month, day in earnings_windows:
        try:
            report_date = date(today.year, month, day)
        except ValueError:
            continue
        # Also check next year's dates
        for yr_offset in [0, 1]:
            try:
                d = date(today.year + yr_offset, month, day)
            except ValueError:
                continue
            if today <= d <= end:
                d_str = d.isoformat()
                events.setdefault(d_str, []).append({
                    "name": f"TW Companies: ~{quarter} Earnings Window",
                    "type": "earnings",
                    "ticker": "TW-ALL",
                    "region": "TW",
                })
                found += 1

    print(f"  TW: generated {found} event(s)")
    return events


# ---------------------------------------------------------------------------
# KR: DART API for upcoming disclosures
# ---------------------------------------------------------------------------

def fetch_kr_events(kr_companies, days_ahead=60):
    """Fetch upcoming Korean disclosure events from DART API.
    Korean companies follow standard reporting windows:
    - Q1 (Jan-Mar): by May 15
    - H1 (Apr-Jun): by Aug 14
    - Q3 (Jul-Sep): by Nov 14
    - FY (Oct-Dec): by Mar 31
    Also check recent DART filings for actual scheduled dates.
    """
    import os
    events = {}
    today = datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
    end = today + timedelta(days=days_ahead)
    found = 0

    print(f"  KR: checking DART for upcoming disclosures...")

    api_key = os.getenv("DART_API_KEY")
    if not api_key:
        print("  KR: DART_API_KEY not set, using estimated dates only")
    else:
        # Search for recent filings to detect timing patterns
        # DART list API can show recently filed reports
        for ticker, info in kr_companies.items():
            corp_code = info.get("dart_code")
            if not corp_code:
                continue

            try:
                resp = http_requests.get(
                    "https://opendart.fss.or.kr/api/list.json",
                    params={
                        "crtfc_key": api_key,
                        "corp_code": corp_code,
                        "bgn_de": (today - timedelta(days=7)).strftime("%Y%m%d"),
                        "end_de": (today + timedelta(days=days_ahead)).strftime("%Y%m%d"),
                        "pblntf_ty": "A",
                        "page_count": "10",
                    },
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "000":
                        for item in data.get("list", []):
                            rcept_dt = item.get("rcept_dt", "")
                            report_nm = item.get("report_nm", "")
                            if len(rcept_dt) == 8:
                                d_str = f"{rcept_dt[:4]}-{rcept_dt[4:6]}-{rcept_dt[6:8]}"
                                try:
                                    d = date.fromisoformat(d_str)
                                except ValueError:
                                    continue
                                if today <= d <= end:
                                    # Determine event type from report name
                                    if "사업보고서" in report_nm:
                                        label = f"{ticker}: Annual Report Filed"
                                    elif "반기보고서" in report_nm:
                                        label = f"{ticker}: Semi-Annual Report Filed"
                                    elif "분기보고서" in report_nm:
                                        label = f"{ticker}: Quarterly Report Filed"
                                    else:
                                        continue
                                    events.setdefault(d_str, []).append({
                                        "name": label,
                                        "type": "earnings",
                                        "ticker": ticker,
                                        "region": "KR",
                                    })
                                    found += 1
                time.sleep(REQUEST_DELAY)
            except Exception:
                pass

    # Standard Korean reporting deadlines
    kr_deadlines = [
        ("FY Annual Report", 3, 31),
        ("Q1 Quarterly Report", 5, 15),
        ("H1 Semi-Annual Report", 8, 14),
        ("Q3 Quarterly Report", 11, 14),
    ]

    for label, month, day in kr_deadlines:
        for yr_offset in [0, 1]:
            try:
                d = date(today.year + yr_offset, month, day)
            except ValueError:
                continue
            if today <= d <= end:
                d_str = d.isoformat()
                events.setdefault(d_str, []).append({
                    "name": f"KR Companies: ~{label} Deadline",
                    "type": "earnings",
                    "ticker": "KR-ALL",
                    "region": "KR",
                })
                found += 1

    print(f"  KR: found {found} event(s)")
    return events


# ---------------------------------------------------------------------------
# Main: fetch all and merge
# ---------------------------------------------------------------------------

def _merge_events(base, new):
    """Merge new events into base, avoiding exact duplicates."""
    for d_str, evts in new.items():
        existing = base.setdefault(d_str, [])
        existing_keys = {(e["name"], e.get("ticker", "")) for e in existing}
        for evt in evts:
            key = (evt["name"], evt.get("ticker", ""))
            if key not in existing_keys:
                existing.append(evt)
                existing_keys.add(key)


def refresh_events(days_ahead=60, force=False):
    """Fetch events from all sources and cache the result.
    Returns the merged events dict {date_str: [event, ...]}."""
    from dotenv import load_dotenv
    load_dotenv()

    # Check cache
    if not force:
        cached, fresh = _load_cache()
        if fresh:
            print("Events cache is fresh, skipping refresh.")
            return cached

    by_market = _companies_by_market()
    all_events = {}

    # US
    if "US" in by_market:
        us_events = fetch_us_earnings(by_market["US"], days_ahead)
        _merge_events(all_events, us_events)

    # Japan
    if "JP" in by_market:
        jp_events = fetch_jp_earnings(by_market["JP"], days_ahead)
        _merge_events(all_events, jp_events)

    # Taiwan
    if "TW" in by_market:
        tw_events = fetch_tw_events(by_market["TW"], days_ahead)
        _merge_events(all_events, tw_events)

    # Korea
    if "KR" in by_market:
        kr_events = fetch_kr_events(by_market["KR"], days_ahead)
        _merge_events(all_events, kr_events)

    # Sort events within each date
    for d_str in all_events:
        all_events[d_str].sort(key=lambda e: (e.get("region", ""), e.get("name", "")))

    _save_cache(all_events)

    total = sum(len(v) for v in all_events.values())
    dates_with_events = len(all_events)
    print(f"\n=== Events calendar refreshed ===")
    print(f"Total: {total} events across {dates_with_events} dates")
    print(f"Cached to: {CACHE_PATH}")

    return all_events


_refresh_lock = False


def get_events():
    """Get events from cache. If stale, triggers a background refresh and
    returns the stale data immediately — next page load picks up fresh data."""
    global _refresh_lock
    cached, fresh = _load_cache()
    if fresh:
        return cached

    # Trigger background refresh (non-blocking)
    if not _refresh_lock:
        _refresh_lock = True
        import threading

        def _bg_refresh():
            global _refresh_lock
            try:
                refresh_events(days_ahead=60, force=True)
            except Exception as e:
                print(f"Events background refresh failed: {e}")
            finally:
                _refresh_lock = False

        t = threading.Thread(target=_bg_refresh, daemon=True)
        t.start()

    return cached


if __name__ == "__main__":
    import sys
    force = "--force" in sys.argv
    days = 60
    for arg in sys.argv[1:]:
        if arg.startswith("--days="):
            days = int(arg.split("=")[1])
    refresh_events(days_ahead=days, force=force)
