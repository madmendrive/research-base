"""Taiwan MOPS downloader — fetches financial statements from the Market
Observation Post System (mops.twse.com.tw) Vue.js SPA.

For each company + year/quarter, navigates the SPA form, renders the
financial statement as PDF, and organizes into data/{ticker}/filings/.
"""

import json
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_PATH = PROJECT_ROOT / "config" / "companies.json"

REQUEST_DELAY = 2
_ROC_OFFSET = 1911

_MONTH_TO_NUM = {
    "January": 1, "February": 2, "March": 3, "April": 4,
    "May": 5, "June": 6, "July": 7, "August": 8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}


def _load_companies():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _extract_stock_code(ticker):
    return ticker.split()[0]


def _quarter_to_fiscal(year, quarter, fy_end_month):
    if fy_end_month == 12:
        if quarter == 4:
            return "FY", year
        return f"{quarter}Q", year
    else:
        q_end_months = {1: 3, 2: 6, 3: 9, 4: 12}
        q_end = q_end_months[quarter]
        offset = (q_end - fy_end_month) % 12
        if offset == 0:
            fq = 4
        elif offset <= 3:
            fq = 1
        elif offset <= 6:
            fq = 2
        else:
            fq = 3
        fy = year + 1 if q_end > fy_end_month else year
        if fq == 4:
            return "FY", fy
        return f"{fq}Q", fy


def _fetch_mops_statement(ctx, stock_code, roc_year, quarter):
    """Fetch one financial statement from MOPS. Returns PDF bytes or None."""
    page = ctx.new_page()
    try:
        page.goto("https://mops.twse.com.tw/mops/#/web/t164sb03", timeout=30000)
        page.wait_for_selector("#companyId", timeout=15000)
        time.sleep(1)

        # Fill company ID
        page.fill("#companyId", stock_code)

        # Click "自訂" (custom date) label to reveal year/season fields
        page.evaluate("""() => {
            const labels = document.querySelectorAll('label');
            for (const l of labels) {
                if (l.textContent.includes('自訂')) { l.click(); break; }
            }
        }""")
        time.sleep(0.5)

        if not page.is_visible("#year"):
            page.close()
            return None

        page.fill("#year", str(roc_year))
        page.select_option("#season", str(quarter))

        # Click search
        search_btn = page.query_selector("#searchBtn")
        if not search_btn:
            page.close()
            return None

        search_btn.click()
        time.sleep(3)

        # Check if we got data
        text = page.inner_text("body")
        has_data = any(kw in text for kw in ["資產", "負債", "營收", "合計", "Total"])
        has_no_data = "查無資料" in text or "查無相關" in text

        if has_no_data or not has_data:
            page.close()
            return None

        # Render the financial statement as PDF
        pdf_bytes = page.pdf(format="A4", print_background=True)
        page.close()
        return pdf_bytes

    except Exception as e:
        try:
            page.close()
        except Exception:
            pass
        return None


def _organize_mops_filings(ticker, mops_dir):
    """Organize downloaded MOPS files into data/{ticker}/filings/."""
    companies = _load_companies()
    company = companies.get(ticker, {})
    fy_end_name = company.get("fiscal_year_end", "December")
    fy_end_month = _MONTH_TO_NUM.get(fy_end_name, 12)

    filings_dir = DATA_DIR / ticker / "filings"
    index = []

    for pdf in sorted(mops_dir.glob("*.pdf")):
        m = re.match(r"(\d{4})Q(\d)\.pdf", pdf.name)
        if not m:
            continue

        greg_year = int(m.group(1))
        quarter = int(m.group(2))

        label, fy = _quarter_to_fiscal(greg_year, quarter, fy_end_month)
        fy_short = f"{fy % 100:02d}"

        if label == "FY":
            form_type = "Annual"
            clean_name = f"{ticker}_Annual_FY{fy_short}.pdf"
            out_dir = filings_dir / "Annual"
        else:
            form_type = "Quarterly"
            clean_name = f"{ticker}_Quarterly_{label}FY{fy_short}.pdf"
            out_dir = filings_dir / "Quarterly"

        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / clean_name

        if not dest.exists():
            shutil.copy2(pdf, dest)

        entry = {
            "filename": clean_name,
            "form_type": form_type,
            "calendar_period": f"{greg_year} Q{quarter}",
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

        existing_non_mops = [e for e in existing if not e.get("source", "").startswith("mops")]
        all_entries = existing_non_mops + index

        index_data = {
            "ticker": ticker,
            "fiscal_year_end": fy_end_name,
            "organized_at": datetime.now(timezone.utc).isoformat(),
            "filings": all_entries,
            "summary": {
                "Annual": len([e for e in all_entries if e["form_type"] == "Annual"]),
                "Quarterly": len([e for e in all_entries if e["form_type"] == "Quarterly"]),
                "total": len(all_entries),
            },
        }
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index_data, f, indent=2, ensure_ascii=False)
            f.write("\n")


def _download_monthly_revenue(ctx, stock_code, mops_dir, since_year):
    """Download individual monthly revenue reports from emops.twse.com.tw."""
    rev_dir = mops_dir / "monthly_revenue"
    rev_dir.mkdir(parents=True, exist_ok=True)

    current_year = datetime.now().year
    current_month = datetime.now().month
    downloaded = 0
    skipped = 0

    for year in range(current_year, since_year - 1, -1):
        for month in range(12, 0, -1):
            # Skip future months
            if year == current_year and month >= current_month:
                continue

            dest_name = f"{year}_{month:02d}.pdf"
            dest_path = rev_dir / dest_name
            if dest_path.exists():
                skipped += 1
                continue

            url = f"https://emops.twse.com.tw/server-java/t05st10_e?step=show&co_id={stock_code}&year={year}&month={month}"
            page = ctx.new_page()
            try:
                page.goto(url, timeout=15000)
                time.sleep(1)

                body_text = page.inner_text("body")
                if len(body_text) > 100 and ("Revenue" in body_text or "Invoice" in body_text):
                    pdf_bytes = page.pdf(format="A4", print_background=True)
                    if pdf_bytes and len(pdf_bytes) > 1000:
                        dest_path.write_bytes(pdf_bytes)
                        downloaded += 1
            except Exception:
                pass
            finally:
                page.close()
            time.sleep(0.5)

    if downloaded:
        print(f"    Downloaded {downloaded} monthly revenue reports ({skipped} already existed)")


def download_mops_filings(ticker, since_year=2020):
    """Main entry point: download MOPS filings for a Taiwan-listed company."""
    companies = _load_companies()
    company = companies.get(ticker, {})

    if company.get("market") != "TW":
        return

    stock_code = company.get("twse_code") or _extract_stock_code(ticker)
    if not stock_code:
        print(f"No TWSE code configured for {ticker}, skipping MOPS.")
        return

    print(f"\n=== MOPS Downloader: {ticker} — {company.get('name', '?')} ===")
    print(f"TWSE code: {stock_code}")

    mops_dir = DATA_DIR / ticker / "mops"
    mops_dir.mkdir(parents=True, exist_ok=True)

    current_year = datetime.now().year
    roc_start = since_year - _ROC_OFFSET
    roc_end = current_year - _ROC_OFFSET

    print(f"  Scanning ROC {roc_start}-{roc_end}...")

    from playwright.sync_api import sync_playwright

    downloaded = 0
    skipped = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            ignore_https_errors=True,
        )

        for roc_year in range(roc_end, roc_start - 1, -1):
            greg_year = roc_year + _ROC_OFFSET

            for quarter in (4, 3, 2, 1):
                dest_name = f"{greg_year}Q{quarter}.pdf"
                dest_path = mops_dir / dest_name

                if dest_path.exists():
                    skipped += 1
                    continue

                pdf_bytes = _fetch_mops_statement(ctx, stock_code, roc_year, quarter)

                if pdf_bytes and len(pdf_bytes) > 5000:
                    dest_path.write_bytes(pdf_bytes)
                    print(f"    Downloaded: {dest_name} ({len(pdf_bytes)//1024} KB)")
                    downloaded += 1
                else:
                    pass  # No data for this period

                time.sleep(REQUEST_DELAY)

        # Also download monthly revenue reports
        print(f"\n  Downloading monthly revenue reports...")
        _download_monthly_revenue(ctx, stock_code, mops_dir, since_year)

        browser.close()

    print(f"\n  Downloaded {downloaded} new quarterly filings, {skipped} already existed.")

    # Organize
    all_pdfs = list(mops_dir.glob("*.pdf"))
    if all_pdfs:
        print(f"\n=== Organizing MOPS filings: {ticker} ===")
        _organize_mops_filings(ticker, mops_dir)
        total = len(list((DATA_DIR / ticker / "filings").rglob("*.pdf")))
        print(f"\n=== Organized {total} filing(s) ===")
    else:
        print("  No MOPS filings to organize.")
