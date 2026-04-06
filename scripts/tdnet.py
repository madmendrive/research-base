"""Japan TDNet downloader — fetches 決算短信 (earnings summaries/tanshin)
and other timely disclosures from TDNet.

TDNet has no API. We scrape the disclosure list pages by date, filter for
the target company's securities code, and download the PDF attachments.

TDNet only keeps ~30 days of disclosures, so this is best for recent filings.
"""

import json
import re
import shutil
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_PATH = PROJECT_ROOT / "config" / "companies.json"

TDNET_BASE = "https://www.release.tdnet.info/inbs"
REQUEST_DELAY = 2  # 2 seconds between page loads

# Keywords to filter disclosures
TANSHIN_KEYWORDS = ["決算短信", "業績予想の修正", "配当予想の修正"]

_MONTH_TO_NUM = {
    "January": 1, "February": 2, "March": 3, "April": 4,
    "May": 5, "June": 6, "July": 7, "August": 8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}


def _load_companies():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _extract_sec_code(ticker):
    """Extract 5-digit TDNet securities code from ticker.
    e.g. '4004 JT' → '40040'
    """
    code = ticker.split()[0]
    # TDNet uses 5-digit codes (4-digit + check digit, usually 0)
    if len(code) == 4:
        return code + "0"
    return code


def _parse_tanshin_period(title, fy_end_month):
    """Parse fiscal period from tanshin title.

    e.g. '2025年12月期 決算短信〔IFRS〕（連結）' → ('FY', 2025)
         '2025年12月期 第1四半期決算短信〔IFRS〕（連結）' → ('1Q', 2025)
         '2025年12月期 第3四半期決算短信〔IFRS〕（連結）' → ('3Q', 2025)
    """
    # Match year and period end month
    m = re.search(r'(\d{4})年(\d{1,2})月期', title)
    if not m:
        return None, None

    year = int(m.group(1))

    # Check for quarterly
    q_match = re.search(r'第(\d)四半期', title)
    if q_match:
        quarter = int(q_match.group(1))
        return f"{quarter}Q", year

    # Full year tanshin
    return "FY", year


def _scan_date(ctx, search_date, sec_code):
    """Scan all pages of a TDNet date listing for disclosures matching sec_code.

    Returns list of dicts with title, pdf_urls, date.
    """
    results = []
    dt_str = search_date.strftime("%Y%m%d")

    for pg in range(1, 100):
        url = f"{TDNET_BASE}/I_list_{pg:03d}_{dt_str}.html"
        page = ctx.new_page()
        try:
            resp = page.goto(url, timeout=15000)
            time.sleep(1)

            text = page.inner_text("body")
            if "Not Found" in text or not resp or not resp.ok:
                page.close()
                break

            # Check if our sec code appears on this page
            src = page.content()
            if sec_code not in src:
                page.close()
                time.sleep(REQUEST_DELAY)
                continue

            # Parse rows for matching disclosures
            rows = page.query_selector_all("tr")
            for row in rows:
                cells = row.query_selector_all("td")
                if len(cells) < 4:
                    continue

                code_text = cells[1].inner_text().strip()
                if code_text != sec_code:
                    continue

                company = cells[2].inner_text().strip()
                title = cells[3].inner_text().strip()

                # Get PDF links from this row
                pdf_links = row.query_selector_all('a[href$=".pdf"]')
                pdf_urls = []
                for link in pdf_links:
                    href = link.get_attribute("href") or ""
                    if href:
                        pdf_urls.append(href)

                results.append({
                    "date": search_date.isoformat(),
                    "code": code_text,
                    "company": company,
                    "title": title,
                    "pdf_urls": pdf_urls,
                })

            page.close()
            time.sleep(REQUEST_DELAY)

        except Exception as e:
            page.close()
            break

    return results


def _download_pdf(ctx, pdf_filename, dest_path):
    """Download a PDF from TDNet."""
    url = f"{TDNET_BASE}/{pdf_filename}"
    page = ctx.new_page()
    try:
        # Try expect_download first (TDNet might trigger download)
        try:
            with page.expect_download(timeout=30000) as dl_info:
                page.goto(url, timeout=30000)
            download = dl_info.value
            download.save_as(str(dest_path))
            page.close()
            return True
        except Exception:
            pass

        # Fallback: read response body
        resp = page.goto(url, timeout=30000)
        if resp and resp.ok:
            body = resp.body()
            if len(body) > 500 and body[:4] == b'%PDF':
                dest_path.write_bytes(body)
                page.close()
                return True

        page.close()
    except Exception:
        page.close()

    # Last resort: API request from context
    try:
        resp = ctx.request.get(url, timeout=30000)
        if resp.ok:
            body = resp.body()
            if len(body) > 500:
                dest_path.write_bytes(body)
                return True
    except Exception:
        pass

    return False


def _organize_tdnet_filings(ticker, tdnet_dir):
    """Organize downloaded TDNet files into data/{ticker}/filings/."""
    companies = _load_companies()
    company = companies.get(ticker, {})
    fy_end_name = company.get("fiscal_year_end", "December")
    fy_end_month = _MONTH_TO_NUM.get(fy_end_name, 12)

    filings_dir = DATA_DIR / ticker / "filings"
    index = []

    # Read download log
    log_path = tdnet_dir / "download_log.json"
    log_entries = {}
    if log_path.exists():
        with open(log_path, encoding="utf-8") as f:
            for e in json.load(f):
                for pdf in e.get("downloaded_files", []):
                    log_entries[Path(pdf).name] = e

    for pdf in sorted(tdnet_dir.glob("*.pdf")):
        log_entry = log_entries.get(pdf.name, {})
        title = log_entry.get("title", "")

        # Only organize tanshin (決算短信) files
        if not any(kw in title for kw in ["決算短信"]):
            continue

        label, fy = _parse_tanshin_period(title, fy_end_month)
        if not label or not fy:
            continue

        fy_short = f"{fy % 100:02d}"

        if label == "FY":
            clean_name = f"{ticker}_Tanshin_FY{fy_short}.pdf"
        else:
            clean_name = f"{ticker}_Tanshin_{label}FY{fy_short}.pdf"

        out_dir = filings_dir / "Tanshin"
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / clean_name

        if not dest.exists():
            shutil.copy2(pdf, dest)

        entry = {
            "filename": clean_name,
            "form_type": "Tanshin",
            "title": title,
            "filing_date": log_entry.get("date", ""),
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
                with open(index_path, encoding="utf-8") as f:
                    existing = json.load(f).get("filings", [])
            except Exception:
                pass

        existing_non_tdnet = [
            e for e in existing if not e.get("source", "").startswith("tdnet")
        ]
        all_entries = existing_non_tdnet + index

        index_data = {
            "ticker": ticker,
            "fiscal_year_end": fy_end_name,
            "organized_at": datetime.now(timezone.utc).isoformat(),
            "filings": all_entries,
            "summary": {
                "Tanshin": len([e for e in all_entries if e.get("form_type") == "Tanshin"]),
                "total": len(all_entries),
            },
        }
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index_data, f, indent=2, ensure_ascii=False)
            f.write("\n")


def download_tdnet_filings(ticker, since_year=2020):
    """Main entry point: download TDNet disclosures for a Japanese company.

    Note: TDNet only keeps ~30 days of disclosures, so this only gets
    recent filings. Older tanshin should come from the IR website or EDINET.
    """
    companies = _load_companies()
    company = companies.get(ticker, {})

    if company.get("market") != "JP":
        return

    sec_code = _extract_sec_code(ticker)
    print(f"\n=== TDNet Downloader: {ticker} — {company.get('name', '?')} ===")
    print(f"Securities code: {sec_code}")

    tdnet_dir = DATA_DIR / ticker / "tdnet"
    tdnet_dir.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            accept_downloads=True,
        )

        # Scan the last 30 days (TDNet's approximate retention)
        end_date = date.today()
        start_date = end_date - timedelta(days=30)
        # Don't go earlier than since_year
        earliest = date(since_year, 1, 1)
        if start_date < earliest:
            start_date = earliest

        print(f"  Scanning {start_date} to {end_date}...")

        all_disclosures = []
        d = end_date

        while d >= start_date:
            if d.weekday() < 5:  # Business days only
                results = _scan_date(ctx, d, sec_code)
                if results:
                    for r in results:
                        safe_title = r["title"].encode("ascii", errors="replace").decode()
                        print(f"    {r['date']} | {safe_title[:60]} | {len(r['pdf_urls'])} PDF(s)")
                    all_disclosures.extend(results)
            d -= timedelta(days=1)

        print(f"  Found {len(all_disclosures)} disclosure(s).")

        # Download PDFs
        download_log = []
        downloaded = 0

        for disc in all_disclosures:
            title = disc["title"]
            disc_date = disc["date"].replace("-", "")

            # Filter: only download tanshin and related
            is_relevant = any(kw in title for kw in TANSHIN_KEYWORDS)
            if not is_relevant:
                continue

            disc_files = []
            for pdf_url in disc["pdf_urls"]:
                pdf_filename = pdf_url.split("/")[-1] if "/" in pdf_url else pdf_url
                dest_path = tdnet_dir / pdf_filename

                if dest_path.exists():
                    disc_files.append(str(dest_path.relative_to(DATA_DIR / ticker)))
                    continue

                safe_title = title.encode("ascii", errors="replace").decode()
                print(f"\n  Downloading: {safe_title[:50]}...")
                print(f"    File: {pdf_filename}")

                if _download_pdf(ctx, pdf_filename, dest_path):
                    size_kb = round(dest_path.stat().st_size / 1024)
                    print(f"    Saved ({size_kb} KB)")
                    disc_files.append(str(dest_path.relative_to(DATA_DIR / ticker)))
                    downloaded += 1
                else:
                    print(f"    Failed")

                time.sleep(REQUEST_DELAY)

            disc["downloaded_files"] = disc_files
            download_log.append(disc)

        browser.close()

    # Save download log
    log_path = tdnet_dir / "download_log.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(download_log, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"\n  Downloaded {downloaded} new file(s).")
    total_pdfs = len(list(tdnet_dir.glob("*.pdf")))
    print(f"  Total PDFs in tdnet/: {total_pdfs}")

    # Organize
    if total_pdfs > 0:
        print(f"\n=== Organizing TDNet filings: {ticker} ===")
        _organize_tdnet_filings(ticker, tdnet_dir)
        tanshin_count = len(list((DATA_DIR / ticker / "filings" / "Tanshin").glob("*.pdf"))) if (DATA_DIR / ticker / "filings" / "Tanshin").exists() else 0
        print(f"\n=== Organized {tanshin_count} Tanshin filing(s) ===")
    else:
        print("  No TDNet filings to organize.")
