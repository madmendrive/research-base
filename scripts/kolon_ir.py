"""Kolon Industries (120110 KS) IR materials downloader.

The English IR page (/en/invest/ir) blocks automated scraping, but the Korean
page (/kr/invest/ir) exposes a clean POST API for the materials list and
direct file URLs on each detail page. This downloader captures the earnings
presentation decks (실적 설명회 자료) that DART does NOT contain.

Endpoints:
- POST /kr/invest/irSelect          → HTML fragment with item rows
- GET  /kr/invest/irDtl?detailsKey  → detail page with /file/view links
- GET  /file/view?fileSeq&fileOrd   → file download
"""

import json
import re
import time
import urllib3
from pathlib import Path

import requests
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

BASE = "https://www.kolonindustries.com"
LIST_URL = f"{BASE}/kr/invest/ir"
LIST_API = f"{BASE}/kr/invest/irSelect"
DETAIL_URL = f"{BASE}/kr/invest/irDtl"
REQUEST_DELAY = 0.5

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"


def _safe_filename(name):
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip()


def _new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    s.verify = False
    r = s.get(LIST_URL, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    meta = soup.find("meta", attrs={"name": "_csrf"})
    inp = soup.find("input", attrs={"name": "_csrf"})
    csrf = (meta and meta.get("content")) or (inp and inp.get("value")) or ""
    return s, csrf


def _list_items(session, csrf):
    """Return [{'detailsKey': '4032', 'title': '...', 'date': '2026.02.27'}, ...]."""
    r = session.post(
        LIST_API,
        data={"listRowSize": "200", "pageIndex": "1", "_csrf": csrf},
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": LIST_URL},
        timeout=30,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for a in soup.select("a.listView, a.list.listView"):
        key = a.get("data-details-key")
        title_el = a.select_one(".page_sub_title_Bold")
        date_el = a.select_one(".date")
        if not key or not title_el:
            continue
        items.append({
            "detailsKey": key,
            "title": title_el.get_text(strip=True),
            "date": date_el.get_text(strip=True) if date_el else "",
        })
    return items


def _detail_files(session, details_key):
    """Return [(fileSeq, fileOrd, filename), ...] for a detail page."""
    r = session.get(DETAIL_URL, params={"detailsKey": details_key}, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    files = []
    for a in soup.find_all("a", href=re.compile(r"file/view\?fileSeq=")):
        m = re.search(r"fileSeq=(\d+).*?fileOrd=(\d+)", a["href"])
        if not m:
            continue
        files.append((m.group(1), m.group(2), a.get_text(strip=True)))
    return files


def _download_file(session, file_seq, file_ord, dest):
    r = session.get(
        f"{BASE}/file/view",
        params={"fileSeq": file_seq, "fileOrd": file_ord},
        headers={"Referer": LIST_URL},
        timeout=120,
        stream=True,
    )
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if chunk:
                f.write(chunk)
    return dest.stat().st_size


def download_kolon_ir(ticker="120110 KS", since_year=2020):
    """Download all Kolon IR materials posted on or after Jan 1 of since_year."""
    out_dir = DATA_DIR / ticker / "ir"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "kolon_log.json"
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else {"items": []}
    seen = {(it["detailsKey"], it["fileSeq"], it["fileOrd"]) for it in log["items"]}

    session, csrf = _new_session()
    items = _list_items(session, csrf)
    print(f"[kolon_ir] {len(items)} items in materials room")

    new_count = 0
    for item in items:
        year_match = re.match(r"(\d{4})", item["date"])
        if year_match and int(year_match.group(1)) < since_year:
            continue
        time.sleep(REQUEST_DELAY)
        files = _detail_files(session, item["detailsKey"])
        if not files:
            print(f"  - [{item['date']}] {item['title']}: no files")
            continue
        for file_seq, file_ord, filename in files:
            key = (item["detailsKey"], file_seq, file_ord)
            if key in seen:
                continue
            safe_name = _safe_filename(filename)
            date_prefix = item["date"].replace(".", "")
            dest = out_dir / f"{date_prefix}_{item['detailsKey']}_{safe_name}"
            try:
                size = _download_file(session, file_seq, file_ord, dest)
            except Exception as e:
                print(f"  ! {item['title']} :: {filename} → {e}")
                continue
            if size < 1024:
                dest.unlink(missing_ok=True)
                print(f"  ! {filename}: empty download, skipped")
                continue
            log["items"].append({
                "detailsKey": item["detailsKey"],
                "title": item["title"],
                "date": item["date"],
                "fileSeq": file_seq,
                "fileOrd": file_ord,
                "filename": filename,
                "local_path": str(dest),
                "size": size,
            })
            seen.add(key)
            new_count += 1
            print(f"  + [{item['date']}] {filename} ({size:,} bytes)")
            time.sleep(REQUEST_DELAY)

    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[kolon_ir] {new_count} new file(s) downloaded; {len(log['items'])} total tracked")
    return new_count


if __name__ == "__main__":
    import sys
    since = int(sys.argv[1]) if len(sys.argv) > 1 else 2020
    download_kolon_ir(since_year=since)
