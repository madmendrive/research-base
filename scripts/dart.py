"""Korea DART downloader — fetches annual, semi-annual, and quarterly reports
from the DART (Data Analysis, Retrieval and Transfer System) API.

Workflow:
1. Search for filings via the DART list API (type "A" = regular reports)
2. Filter for 사업보고서 (annual), 반기보고서 (semi-annual), 분기보고서 (quarterly)
3. Get dcmNo from the filing viewer page
4. Download PDFs from dart.fss.or.kr/pdf/download/pdf.do
5. Organize into data/{ticker}/filings/ with fiscal-period naming
"""

import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import requests as http_requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_PATH = PROJECT_ROOT / "config" / "companies.json"

DART_API_BASE = "https://opendart.fss.or.kr/api"
DART_VIEWER_BASE = "https://dart.fss.or.kr"
REQUEST_DELAY = 1.0  # 1 second between API calls

# Report name patterns → filing type mapping
REPORT_TYPES = {
    "사업보고서": "Annual",       # Annual business report (10-K equivalent)
    "반기보고서": "SemiAnnual",   # Semi-annual report
    "분기보고서": "Quarterly",    # Quarterly report (10-Q equivalent)
}

_MONTH_TO_NUM = {
    "January": 1, "February": 2, "March": 3, "April": 4,
    "May": 5, "June": 6, "July": 7, "August": 8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}


def _load_companies():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _parse_report_type(report_nm):
    """Parse the Korean report name to determine filing type.
    e.g. '사업보고서 (2024.12)' → ('Annual', '2024.12')
    """
    for kr_name, filing_type in REPORT_TYPES.items():
        if kr_name in report_nm:
            # Extract period in parentheses, e.g. (2024.12) or (2025.03)
            m = re.search(r'\((\d{4})\.(\d{2})\)', report_nm)
            period_str = m.group(0) if m else ""
            return filing_type, period_str
    return None, None


def _period_to_fiscal(report_type, period_str, fy_end_month):
    """Map report type + period string to fiscal period label.

    Args:
        report_type: 'Annual', 'SemiAnnual', or 'Quarterly'
        period_str: e.g. '(2024.12)' or '(2025.03)'
        fy_end_month: month number of fiscal year end

    Returns:
        (label, fy_year) e.g. ('FY', 2024) or ('1Q', 2025)
    """
    m = re.search(r'(\d{4})\.(\d{2})', period_str)
    if not m:
        return None, None

    year = int(m.group(1))
    month = int(m.group(2))

    if report_type == "Annual":
        # Annual report period end = FY end
        return "FY", year

    if report_type == "SemiAnnual":
        # Map to H1 or H2
        offset = (month - fy_end_month) % 12
        if offset <= 6:
            half = 1
        else:
            half = 2
        # Fiscal year
        if month > fy_end_month:
            fy = year + 1
        else:
            fy = year
        return f"H{half}", fy

    if report_type == "Quarterly":
        # Map month to fiscal quarter
        offset = (month - fy_end_month) % 12
        if offset == 0:
            quarter = 4
        elif offset <= 3:
            quarter = 1
        elif offset <= 6:
            quarter = 2
        else:
            quarter = 3

        if month > fy_end_month:
            fy = year + 1
        else:
            fy = year
        return f"{quarter}Q", fy

    return None, None


def _search_filings(api_key, corp_code, since_year):
    """Search DART for regular filings (type A) for a company."""
    bgn_de = f"{since_year}0101"
    end_de = datetime.now().strftime("%Y%m%d")

    all_filings = []
    page_no = 1

    while True:
        resp = http_requests.get(f"{DART_API_BASE}/list.json", params={
            "crtfc_key": api_key,
            "corp_code": corp_code,
            "bgn_de": bgn_de,
            "end_de": end_de,
            "pblntf_ty": "A",
            "page_no": str(page_no),
            "page_count": "100",
        }, timeout=30)
        data = resp.json()

        if data.get("status") != "000":
            msg = data.get("message", "Unknown error")
            print(f"  DART API error: {msg}")
            break

        items = data.get("list", [])
        if not items:
            break

        for item in items:
            report_nm = item.get("report_nm", "")
            filing_type, period_str = _parse_report_type(report_nm)
            if filing_type:
                all_filings.append({
                    "rcept_no": item["rcept_no"],
                    "rcept_dt": item["rcept_dt"],
                    "report_nm": report_nm,
                    "filing_type": filing_type,
                    "period_str": period_str,
                })

        total_page = int(data.get("total_page", 1))
        if page_no >= total_page:
            break
        page_no += 1
        time.sleep(REQUEST_DELAY)

    return all_filings


def _get_dcm_no(rcept_no, page):
    """Get the dcmNo for the **consolidated** financial statements from the
    DART filing viewer page.

    DART filings have multiple attachments in a dropdown.  The consolidated
    audit report is labelled with '연결' (e.g. 연결감사보고서, 분기연결검토보고서,
    반기연결검토보고서).  We prefer that attachment; if none is found we fall
    back to the first available dcmNo.

    Accepts an existing Playwright page to avoid launching a new browser.
    """
    try:
        page.goto(
            f"{DART_VIEWER_BASE}/dsaf001/main.do?rcpNo={rcept_no}",
            timeout=30000,
        )
        time.sleep(2)

        # Parse the attachment dropdown for consolidated (연결) document
        options = page.query_selector_all("select option")
        consolidated_dcm = None
        fallback_dcm = None

        for opt in options:
            val = opt.get_attribute("value") or ""
            text = opt.text_content().strip()
            m = re.search(r"dcmNo=(\d+)", val)
            if not m:
                continue
            dcm = m.group(1)

            if fallback_dcm is None:
                fallback_dcm = dcm

            # Match consolidated audit reports:
            #   연결감사보고서, 분기연결검토보고서, 반기연결검토보고서
            if "연결" in text:
                consolidated_dcm = dcm
                break

        if consolidated_dcm:
            return consolidated_dcm

        if fallback_dcm:
            print(f"    No consolidated attachment found, using first attachment")
            return fallback_dcm

        # Last resort: parse dcmNo from page HTML
        html = page.content()
        m = re.search(r'dcmNo=(\d+)', html)
        if m:
            return m.group(1)

    except Exception as e:
        safe = str(e).encode("ascii", errors="replace").decode()
        print(f"    Warning: could not get dcmNo for {rcept_no}: {safe[:80]}")

    return None


def _download_pdf(rcept_no, dcm_no, dest_path, page):
    """Download the PDF from DART's PDF download endpoint.
    Accepts an existing Playwright page to avoid launching a new browser."""
    url = f"{DART_VIEWER_BASE}/pdf/download/pdf.do?rcp_no={rcept_no}&dcm_no={dcm_no}"

    try:
        # The URL triggers a file download, so use expect_download
        try:
            with page.expect_download(timeout=120000) as dl_info:
                page.goto(url, timeout=120000)
            download = dl_info.value
            download.save_as(str(dest_path))
            return True
        except Exception:
            pass

        # Fallback: try reading the response body directly
        try:
            resp = page.context.request.get(url, timeout=120000)
            if resp.ok:
                body = resp.body()
                if len(body) > 1000:
                    dest_path.write_bytes(body)
                    return True
        except Exception:
            pass

    except Exception as e:
        safe = str(e).encode("ascii", errors="replace").decode()
        print(f"    Download error: {safe[:80]}")

    return False


def _organize_dart_filings(ticker, dart_dir):
    """Organize downloaded DART files into data/{ticker}/filings/."""
    companies = _load_companies()
    company = companies.get(ticker, {})
    fy_end_name = company.get("fiscal_year_end", "December")
    fy_end_month = _MONTH_TO_NUM.get(fy_end_name, 12)

    filings_dir = DATA_DIR / ticker / "filings"
    index = []

    # Read the download log for metadata
    log_path = dart_dir / "download_log.json"
    log_entries = {}
    if log_path.exists():
        with open(log_path) as f:
            log_entries = {e["rcept_no"]: e for e in json.load(f)}

    for pdf in sorted(dart_dir.glob("*.pdf")):
        # Parse rcept_no from filename: {date}_{rcept_no}.pdf
        m = re.match(r"(\d{8})_(\d+)\.pdf", pdf.name)
        if not m:
            continue

        date_str = m.group(1)
        rcept_no = m.group(2)

        log_entry = log_entries.get(rcept_no, {})
        filing_type = log_entry.get("filing_type", "Unknown")
        period_str = log_entry.get("period_str", "")

        label, fy = _period_to_fiscal(filing_type, period_str, fy_end_month)
        if not label or not fy:
            continue

        fy_short = f"{fy % 100:02d}"

        if filing_type == "Annual":
            clean_name = f"{ticker}_Annual_FY{fy_short}.pdf"
            out_dir = filings_dir / "Annual"
        elif filing_type == "SemiAnnual":
            clean_name = f"{ticker}_SemiAnnual_{label}FY{fy_short}.pdf"
            out_dir = filings_dir / "SemiAnnual"
        else:
            clean_name = f"{ticker}_Quarterly_{label}FY{fy_short}.pdf"
            out_dir = filings_dir / "Quarterly"

        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / clean_name

        if not dest.exists():
            shutil.copy2(pdf, dest)

        entry = {
            "filename": clean_name,
            "form_type": filing_type,
            "report_nm": log_entry.get("report_nm", ""),
            "filing_date": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}",
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

        existing_non_dart = [
            e for e in existing if not e.get("source", "").startswith("dart")
        ]
        all_entries = existing_non_dart + index

        index_data = {
            "ticker": ticker,
            "fiscal_year_end": fy_end_name,
            "organized_at": datetime.now(timezone.utc).isoformat(),
            "filings": all_entries,
            "summary": {
                "Annual": len([e for e in all_entries if e["form_type"] == "Annual"]),
                "SemiAnnual": len([e for e in all_entries if e["form_type"] == "SemiAnnual"]),
                "Quarterly": len([e for e in all_entries if e["form_type"] == "Quarterly"]),
                "total": len(all_entries),
            },
        }
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index_data, f, indent=2, ensure_ascii=False)
            f.write("\n")


def download_dart_filings(ticker, since_year=2020):
    """Main entry point: download DART filings for a Korean-listed company."""
    api_key = os.getenv("DART_API_KEY")
    if not api_key:
        print("Skipping DART — DART_API_KEY not set in .env")
        return

    companies = _load_companies()
    company = companies.get(ticker, {})

    if company.get("market") != "KR":
        return

    corp_code = company.get("dart_code")
    if not corp_code:
        print(f"No DART code configured for {ticker}, skipping.")
        return

    print(f"\n=== DART Downloader: {ticker} — {company.get('name', '?')} ===")
    print(f"Corp code: {corp_code}")

    dart_dir = DATA_DIR / ticker / "dart"
    dart_dir.mkdir(parents=True, exist_ok=True)

    # 1. Search for filings
    print(f"  Searching for filings since {since_year}...")
    filings = _search_filings(api_key, corp_code, since_year)
    print(f"  Found {len(filings)} filing(s):")
    for f in filings:
        safe_nm = f["report_nm"].encode("ascii", errors="replace").decode()
        print(f"    {f['rcept_dt']} | {f['filing_type']:12s} | {safe_nm}")
    time.sleep(REQUEST_DELAY)

    # 2. Download each filing (single browser session for all filings)
    from playwright.sync_api import sync_playwright

    download_log = []
    downloaded = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(ignore_https_errors=True, accept_downloads=True)
        page = ctx.new_page()

        for filing in filings:
            rcept_no = filing["rcept_no"]
            rcept_dt = filing["rcept_dt"]
            dest_name = f"{rcept_dt}_{rcept_no}.pdf"
            dest_path = dart_dir / dest_name

            if dest_path.exists():
                filing["local_path"] = str(dest_path)
                download_log.append(filing)
                continue

            print(f"\n  Downloading: {filing['report_nm'][:40].encode('ascii', errors='replace').decode()}...")

            # Get dcmNo from the viewer page
            dcm_no = _get_dcm_no(rcept_no, page)
            if not dcm_no:
                print(f"    Could not find dcmNo, skipping.")
                time.sleep(REQUEST_DELAY)
                download_log.append(filing)
                continue

            # Download the PDF
            if _download_pdf(rcept_no, dcm_no, dest_path, page):
                size_kb = round(dest_path.stat().st_size / 1024)
                print(f"    Saved: {dest_name} ({size_kb} KB)")
                filing["local_path"] = str(dest_path)
                downloaded += 1
            else:
                print(f"    Failed to download PDF.")

            download_log.append(filing)
            time.sleep(REQUEST_DELAY)

        browser.close()

    # Save download log
    log_path = dart_dir / "download_log.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(download_log, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"\n  Downloaded {downloaded} new filing(s).")
    total_pdfs = len(list(dart_dir.glob("*.pdf")))
    print(f"  Total PDFs in dart/: {total_pdfs}")

    # 3. Organize into filings/
    if total_pdfs > 0:
        print(f"\n=== Organizing DART filings: {ticker} ===")
        _organize_dart_filings(ticker, dart_dir)
        total_organized = len(list((DATA_DIR / ticker / "filings").rglob("*.pdf")))
        print(f"\n=== Organized {total_organized} filing(s) ===")
