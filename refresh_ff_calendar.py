"""Refresh ForexFactory calendar cache — scrapes red events for the next 3 weeks.
Run manually or on a schedule: python refresh_ff_calendar.py"""
import json
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent
CACHE_PATH = PROJECT_ROOT / "data" / "Macro" / "_ff_calendar_cache.json"


def scrape_ff_weeks():
    from playwright.sync_api import sync_playwright

    today = date.today()
    # Build week start dates for next 3 weeks
    next_monday = today + timedelta(days=(7 - today.weekday()) % 7)
    if next_monday == today and today.weekday() == 0:
        next_monday = today + timedelta(days=7)
    week_mondays = [next_monday + timedelta(weeks=i) for i in range(3)]
    week_strs = [m.strftime("%b%d.%Y").lower() for m in week_mondays]

    all_events = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )

        for week_str in week_strs:
            page = ctx.new_page()
            url = f"https://www.forexfactory.com/calendar?week={week_str}"
            print(f"Fetching {url}...")
            try:
                page.goto(url, timeout=45000)
                time.sleep(8)

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

                print(f"  {len(result)} red events")
                for evt in result:
                    date_text = evt.get("date", "").replace("\n", " ").strip()
                    parts = date_text.split()
                    if len(parts) >= 3:
                        try:
                            month_str = parts[-2]
                            day_str = parts[-1]
                            dt = datetime.strptime(f"{month_str} {day_str} {today.year}", "%b %d %Y")
                            d = dt.strftime("%Y-%m-%d")
                            detail = f"{evt['currency']} {evt['title']}"
                            all_events.setdefault(d, []).append({
                                "name": detail, "type": "macro", "region": evt["currency"],
                            })
                        except Exception:
                            pass
            except Exception as e:
                print(f"  Error: {str(e)[:80]}")
            finally:
                page.close()
                time.sleep(3)

        browser.close()

    return all_events


if __name__ == "__main__":
    events = scrape_ff_weeks()
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(events, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"\nCached {sum(len(v) for v in events.values())} events across {len(events)} dates")
    for d in sorted(events.keys()):
        print(f"  {d}: {len(events[d])} events")
        for e in events[d][:3]:
            print(f"    {e['name']}")
