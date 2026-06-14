"""Japan EDINET downloader — fetches securities reports (有価証券報告書),
quarterly reports (四半期報告書), and semi-annual reports (半期報告書)
from the EDINET API.

The EDINET API only searches by date, so we iterate over business days,
filtering results by the company's edinetCode. A scan_state.json cache
tracks progress so interrupted scans can resume.
"""

import json
import os
import re
import shutil
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests as http_requests
from concurrent.futures import ThreadPoolExecutor, as_completed

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_PATH = PROJECT_ROOT / "config" / "companies.json"

EDINET_API_BASE = "https://api.edinet-fsa.go.jp/api/v2"
REQUEST_DELAY = 0.5

# Document type codes we care about
DOC_TYPE_MAP = {
    "120": ("Annual", "有価証券報告書"),
    "130": ("Annual_Amended", "有価証券報告書の訂正"),
    "140": ("Quarterly", "四半期報告書"),
    "150": ("Quarterly_Amended", "四半期報告書の訂正"),
    "160": ("SemiAnnual", "半期報告書"),
}

TARGET_DOC_TYPES = set(DOC_TYPE_MAP.keys())

_MONTH_TO_NUM = {
    "January": 1, "February": 2, "March": 3, "April": 4,
    "May": 5, "June": 6, "July": 7, "August": 8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}


def _load_companies():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _period_to_fiscal(period_end, fy_end_month, filing_type, filing_date=None):
    """Map a period end date string to fiscal period label.

    Args:
        period_end: 'YYYY-MM-DD' period end date from EDINET metadata
        fy_end_month: month number of fiscal year end
        filing_type: 'Annual', 'Quarterly', 'SemiAnnual', etc.
        filing_date: 'YYYY-MM-DD' actual filing date (used for semi-annual)

    Returns:
        (label, fy_year) e.g. ('FY', 2024) or ('1Q', 2025)
    """
    if not period_end:
        return None, None

    try:
        year = int(period_end[:4])
        month = int(period_end[5:7])
    except (ValueError, IndexError):
        return None, None

    if filing_type == "SemiAnnual":
        # EDINET periodEnd for semi-annual is often the full FY end date,
        # not the actual half-year end. Use filing date to determine H1 vs H2:
        # H1 reports are typically filed ~2 months after mid-year.
        if filing_date:
            try:
                f_month = int(filing_date[5:7])
            except (ValueError, IndexError):
                f_month = 0
            # If filed in Jul-Oct, it's H1; if filed in Jan-Apr, it's H2
            if 6 <= f_month <= 11:
                half = 1
            else:
                half = 2
        else:
            offset = (month - fy_end_month) % 12
            half = 1 if offset <= 6 else 2

        # FY is the year the FY ends
        fy = year
        return f"H{half}", fy

    if "Annual" in filing_type:
        return "FY", year

    if "Quarterly" in filing_type:
        offset = (month - fy_end_month) % 12
        if offset == 0:
            quarter = 4
        elif offset <= 3:
            quarter = 1
        elif offset <= 6:
            quarter = 2
        else:
            quarter = 3
        fy = year + 1 if month > fy_end_month else year
        return f"{quarter}Q", fy

    return None, None


def _search_date(api_key, search_date, edinet_code, sec_code=None):
    """Search EDINET for a date's filings by EDINET code or securities code."""
    resp = http_requests.get(f"{EDINET_API_BASE}/documents.json", params={
        "date": search_date,
        "type": "2",
        "Subscription-Key": api_key,
    }, timeout=30)

    data = resp.json()
    if data.get("metadata", {}).get("status") != "200":
        return []

    results = []
    for r in data.get("results", []):
        ec = r.get("edinetCode")
        sc = r.get("secCode") or ""
        # Match by EDINET code when configured, else by the 4-digit securities
        # code derived from the "XXXX JT" ticker (EDINET secCode is 5 chars).
        if edinet_code and ec == edinet_code:
            pass
        elif sec_code and sc[:4] == sec_code:
            pass
        else:
            continue
        doc_type = r.get("docTypeCode") or ""
        if doc_type not in TARGET_DOC_TYPES:
            continue
        results.append({
            "docID": r["docID"],
            "docTypeCode": doc_type,
            "docDescription": r.get("docDescription", ""),
            "filerName": r.get("filerName", ""),
            "periodStart": r.get("periodStart", ""),
            "periodEnd": r.get("periodEnd", ""),
            "submitDateTime": r.get("submitDateTime", ""),
            "pdfFlag": r.get("pdfFlag", "0"),
            "filing_date": search_date,
        })

    return results


def _download_filing(api_key, doc_id, dest_path):
    """Download a filing PDF from EDINET.

    type=2 returns the PDF directly when pdfFlag=1.
    Falls back to type=2 ZIP extraction if needed.
    """
    # Try type=2 first — often returns PDF directly
    resp = http_requests.get(f"{EDINET_API_BASE}/documents/{doc_id}", params={
        "type": "2",
        "Subscription-Key": api_key,
    }, timeout=120)

    if resp.status_code != 200:
        return False

    content = resp.content
    content_type = resp.headers.get("Content-Type", "")

    # Direct PDF
    if content[:4] == b'%PDF':
        dest_path.write_bytes(content)
        return True

    # ZIP file — extract PDF from it
    if content[:2] == b'PK':
        import io
        import zipfile
        try:
            zf = zipfile.ZipFile(io.BytesIO(content))
            pdfs = [n for n in zf.namelist() if n.lower().endswith('.pdf')]
            if pdfs:
                # Use the largest PDF (usually the main document)
                largest = max(pdfs, key=lambda n: zf.getinfo(n).file_size)
                dest_path.write_bytes(zf.read(largest))
                return True

            # No PDF in ZIP — look for main HTML
            htmls = [n for n in zf.namelist()
                     if n.lower().endswith(('.htm', '.html'))
                     and 'XBRL' not in n and 'ix' not in n.lower()]
            if htmls:
                largest_html = max(htmls, key=lambda n: zf.getinfo(n).file_size)
                html_content = zf.read(largest_html)
                # Convert HTML to PDF via Playwright
                return _html_to_pdf(html_content, dest_path)
        except Exception:
            pass

    return False


def _html_to_pdf(html_bytes, dest_path):
    """Convert HTML content to PDF using Playwright."""
    from playwright.sync_api import sync_playwright

    try:
        # Write HTML to temp file
        temp_html = dest_path.with_suffix('.html')
        temp_html.write_bytes(html_bytes)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(ignore_https_errors=True)
            page = ctx.new_page()
            page.goto(f"file:///{temp_html.as_posix()}", timeout=30000)
            time.sleep(1)
            page.pdf(path=str(dest_path), format="A4", print_background=True)
            browser.close()

        temp_html.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _load_scan_state(edinet_dir):
    """Load scan state for resumable date scanning."""
    state_path = edinet_dir / "scan_state.json"
    if state_path.exists():
        with open(state_path) as f:
            return json.load(f)
    return {"last_scanned_date": None, "found_docs": []}


def _save_scan_state(edinet_dir, state):
    state_path = edinet_dir / "scan_state.json"
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def _organize_edinet_filings(ticker, edinet_dir):
    """Organize downloaded EDINET files into data/{ticker}/filings/."""
    companies = _load_companies()
    company = companies.get(ticker, {})
    fy_end_name = company.get("fiscal_year_end", "December")
    fy_end_month = _MONTH_TO_NUM.get(fy_end_name, 12)

    filings_dir = DATA_DIR / ticker / "filings"
    index = []

    # Read download log for metadata
    log_path = edinet_dir / "download_log.json"
    log_entries = {}
    if log_path.exists():
        with open(log_path, encoding="utf-8") as f:
            log_entries = {e["docID"]: e for e in json.load(f)}

    for pdf in sorted(edinet_dir.glob("*.pdf")):
        # Parse from filename: {docTypeCode}_{date}_{docID}.pdf
        m = re.match(r"(\d+)_(\d{4}-\d{2}-\d{2})_(.+)\.pdf", pdf.name)
        if not m:
            continue

        doc_type_code = m.group(1)
        filing_date = m.group(2)
        doc_id = m.group(3)

        filing_type_info = DOC_TYPE_MAP.get(doc_type_code)
        if not filing_type_info:
            continue
        filing_type = filing_type_info[0]

        log_entry = log_entries.get(doc_id, {})
        period_end = log_entry.get("periodEnd", "")

        label, fy = _period_to_fiscal(period_end, fy_end_month, filing_type, filing_date)
        if not label or not fy:
            continue

        fy_short = f"{fy % 100:02d}"

        # Determine clean name and output directory
        base_type = filing_type.replace("_Amended", "")
        amended = "_Amended" if "Amended" in filing_type else ""

        if base_type == "Annual":
            clean_name = f"{ticker}_Annual{amended}_FY{fy_short}.pdf"
            out_dir = filings_dir / "Annual"
        elif base_type == "SemiAnnual":
            clean_name = f"{ticker}_SemiAnnual{amended}_{label}FY{fy_short}.pdf"
            out_dir = filings_dir / "SemiAnnual"
        else:
            clean_name = f"{ticker}_Quarterly{amended}_{label}FY{fy_short}.pdf"
            out_dir = filings_dir / "Quarterly"

        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / clean_name

        if not dest.exists():
            shutil.copy2(pdf, dest)

        entry = {
            "filename": clean_name,
            "form_type": filing_type,
            "filing_date": filing_date,
            "period_end": period_end,
            "fiscal_period": f"{label}FY{fy_short}" if label != "FY" else f"FY{fy_short}",
            "path": str(dest.relative_to(DATA_DIR / ticker)),
            "source": str(pdf.relative_to(DATA_DIR / ticker)),
            "size_kb": round(pdf.stat().st_size / 1024),
        }
        index.append(entry)
        print(f"  {clean_name} ({entry['size_kb']} KB)")

    if index:
        index_path = filings_dir / "index.json"
        existing = []
        if index_path.exists():
            try:
                with open(index_path) as f:
                    existing = json.load(f).get("filings", [])
            except Exception:
                pass

        existing_non_edinet = [
            e for e in existing if not e.get("source", "").startswith("edinet")
        ]
        all_entries = existing_non_edinet + index

        index_data = {
            "ticker": ticker,
            "fiscal_year_end": fy_end_name,
            "organized_at": datetime.now(timezone.utc).isoformat(),
            "filings": all_entries,
            "summary": {
                "Annual": len([e for e in all_entries if "Annual" in e.get("form_type", "")]),
                "Quarterly": len([e for e in all_entries if "Quarterly" in e.get("form_type", "")]),
                "SemiAnnual": len([e for e in all_entries if "SemiAnnual" in e.get("form_type", "")]),
                "total": len(all_entries),
            },
        }
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index_data, f, indent=2, ensure_ascii=False)
            f.write("\n")


def _fetch_day(api_key, search_date, retries=2):
    """Fetch the full EDINET document list for one date (with retry). Returns the
    raw results list, or [] on persistent failure."""
    for attempt in range(retries + 1):
        try:
            resp = http_requests.get(f"{EDINET_API_BASE}/documents.json", params={
                "date": search_date, "type": "2", "Subscription-Key": api_key,
            }, timeout=30)
            data = resp.json()
            if data.get("metadata", {}).get("status") == "200":
                return data.get("results", []) or []
            return []
        except Exception:
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
    return []


def _classify_day(search_date, results, by_edinet, by_sec):
    """Bucket one day's raw EDINET results into (ticker, filing) pairs we keep."""
    out = []
    for r in results:
        doc_type = r.get("docTypeCode") or ""
        if doc_type not in TARGET_DOC_TYPES:
            continue
        ticker = by_edinet.get(r.get("edinetCode")) or by_sec.get((r.get("secCode") or "")[:4])
        if not ticker:
            continue
        out.append((ticker, {
            "docID": r["docID"],
            "docTypeCode": doc_type,
            "docDescription": r.get("docDescription", ""),
            "filerName": r.get("filerName", ""),
            "periodStart": r.get("periodStart", ""),
            "periodEnd": r.get("periodEnd", ""),
            "submitDateTime": r.get("submitDateTime", ""),
            "pdfFlag": r.get("pdfFlag", "0"),
            "filing_date": search_date,
        }))
    return out


def scan_edinet_concurrent(api_key, targets, start_date, end_date, max_workers=8):
    """Scan EDINET once over [start_date, end_date], fanning each day's results out
    to whichever target company they match.

    EDINET's API only lists documents by date, so one shared pass over the range
    serves every company at once — N companies cost one scan, not N. `targets` is
    a list of {"ticker", "edinet_code", "sec_code"}. Returns {ticker: [filing, ...]}.
    """
    by_edinet = {t["edinet_code"]: t["ticker"] for t in targets if t.get("edinet_code")}
    by_sec = {t["sec_code"]: t["ticker"] for t in targets if t.get("sec_code")}
    found = {t["ticker"]: [] for t in targets}

    business_days = []
    d = start_date
    while d <= end_date:
        if d.weekday() < 5:
            business_days.append(d.isoformat())
        d += timedelta(days=1)
    if not business_days:
        return found

    print(f"  Scanning {len(business_days)} business days ({start_date}..{end_date}) "
          f"for {len(targets)} company(ies), {max_workers} parallel...")
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_date = {ex.submit(_fetch_day, api_key, ds): ds for ds in business_days}
        for fut in as_completed(future_to_date):
            ds = future_to_date[fut]
            for ticker, filing in _classify_day(ds, fut.result(), by_edinet, by_sec):
                found[ticker].append(filing)
            done += 1
            if done % 200 == 0:
                print(f"    ...{done}/{len(business_days)} days")
    print(f"  Scan complete: {sum(len(v) for v in found.values())} matching filing(s).")
    return found


def _download_and_organize(ticker, api_key, edinet_dir, all_found, max_workers=4):
    """Download each filing's PDF (in parallel) and organize into filings/."""
    download_log = []
    pending = []
    for filing in all_found:
        dest_name = f"{filing['docTypeCode']}_{filing['filing_date']}_{filing['docID']}.pdf"
        dest_path = edinet_dir / dest_name
        if dest_path.exists():
            download_log.append(filing)
        else:
            pending.append((filing, dest_path))

    downloaded = 0
    if pending:
        def _dl(item):
            filing, dest_path = item
            return filing, dest_path, _download_filing(api_key, filing["docID"], dest_path)
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for filing, dest_path, ok in ex.map(_dl, pending):
                if ok:
                    downloaded += 1
                    print(f"    Saved: {dest_path.name} ({round(dest_path.stat().st_size / 1024)} KB)")
                else:
                    print(f"    Failed: {filing['docID']}")
                download_log.append(filing)

    log_path = edinet_dir / "download_log.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(download_log, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"  Downloaded {downloaded} new filing(s).")
    total_pdfs = len(list(edinet_dir.glob("*.pdf")))
    print(f"  Total PDFs in edinet/: {total_pdfs}")
    if total_pdfs > 0:
        print(f"\n=== Organizing EDINET filings: {ticker} ===")
        _organize_edinet_filings(ticker, edinet_dir)
        total_organized = len(list((DATA_DIR / ticker / "filings").rglob("*.pdf")))
        print(f"=== Organized {total_organized} filing(s) ===")
    return downloaded


def download_edinet_filings_bulk(tickers, since_year=2020, max_workers=8):
    """Scan EDINET for many JP companies in ONE shared pass, then download each.

    The caller assembles the full JP company list up front; we resolve each to its
    EDINET / securities code, sweep the date range once, and bucket every day's
    results to the right company. Resumes from the earliest unscanned date across
    the set, so already-current companies don't force a full re-scan.
    """
    api_key = os.getenv("EDINET_API_KEY")
    if not api_key:
        print("Skipping EDINET — EDINET_API_KEY not set in .env")
        return

    companies = _load_companies()
    end_date = date.today()
    targets, state_by_ticker, earliest = [], {}, end_date
    for ticker in tickers:
        company = companies.get(ticker, {})
        if company.get("market") != "JP":
            continue
        edinet_code = company.get("edinet_code")
        sec_code = None
        if not edinet_code:
            num = ticker.split()[0]
            if num.isdigit():
                sec_code = num
            else:
                continue
        edinet_dir = DATA_DIR / ticker / "edinet"
        edinet_dir.mkdir(parents=True, exist_ok=True)
        st = _load_scan_state(edinet_dir)
        state_by_ticker[ticker] = (edinet_dir, st)
        targets.append({"ticker": ticker, "edinet_code": edinet_code, "sec_code": sec_code})
        if st.get("last_scanned_date"):
            resume = date.fromisoformat(st["last_scanned_date"]) + timedelta(days=1)
        else:
            resume = date(since_year, 1, 1)
        earliest = min(earliest, resume)

    if not targets:
        print("No JP companies to scan for EDINET.")
        return

    start_date = min(earliest, end_date)
    print(f"\n=== EDINET bulk scan: {len(targets)} JP company(ies), {start_date}..{end_date} ===")
    found = scan_edinet_concurrent(api_key, targets, start_date, end_date, max_workers=max_workers)

    for target in targets:
        ticker = target["ticker"]
        edinet_dir, st = state_by_ticker[ticker]
        already = {d["docID"] for d in st.get("found_docs", [])}
        new = [f for f in found[ticker] if f["docID"] not in already]
        st["last_scanned_date"] = end_date.isoformat()
        st["found_docs"] = st.get("found_docs", []) + new
        _save_scan_state(edinet_dir, st)
        if st["found_docs"]:
            print(f"\n--- {ticker}: {len(new)} new, {len(st['found_docs'])} total ---")
            _download_and_organize(ticker, api_key, edinet_dir, st["found_docs"])


def download_edinet_filings(ticker, since_year=2020):
    """Main entry point: download EDINET filings for a Japanese company."""
    api_key = os.getenv("EDINET_API_KEY")
    if not api_key:
        print("Skipping EDINET — EDINET_API_KEY not set in .env")
        return

    companies = _load_companies()
    company = companies.get(ticker, {})

    if company.get("market") != "JP":
        return

    edinet_code = company.get("edinet_code")
    sec_code = None
    if not edinet_code:
        num = ticker.split()[0]
        if num.isdigit():
            sec_code = num  # resolve by securities code from the ticker
        else:
            print(f"No EDINET code and non-numeric ticker for {ticker}, skipping.")
            return

    print(f"\n=== EDINET Downloader: {ticker} — {company.get('name', '?')} ===")
    print(f"  Matching by: {('EDINET code ' + edinet_code) if edinet_code else ('securities code ' + sec_code)}")

    edinet_dir = DATA_DIR / ticker / "edinet"
    edinet_dir.mkdir(parents=True, exist_ok=True)

    # Load scan state for resumable scanning
    state = _load_scan_state(edinet_dir)
    already_found = {d["docID"] for d in state.get("found_docs", [])}

    # Determine scan range
    start_date = date(since_year, 1, 1)
    end_date = date.today()

    # Resume from last scanned date if available
    if state.get("last_scanned_date"):
        resume_date = date.fromisoformat(state["last_scanned_date"]) + timedelta(days=1)
        if resume_date > start_date:
            start_date = resume_date
            print(f"  Resuming scan from {start_date}")

    target = {"ticker": ticker, "edinet_code": edinet_code, "sec_code": sec_code}
    found = scan_edinet_concurrent(api_key, [target], start_date, end_date)
    new_filings = [f for f in found[ticker] if f["docID"] not in already_found]
    state["last_scanned_date"] = end_date.isoformat()
    all_found = state.get("found_docs", []) + new_filings
    state["found_docs"] = all_found
    _save_scan_state(edinet_dir, state)
    print(f"  Found {len(all_found)} filing(s) total ({len(new_filings)} new).")

    _download_and_organize(ticker, api_key, edinet_dir, all_found)
