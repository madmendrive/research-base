"""SEC EDGAR downloader — fetches 10-K, 10-Q, 8-K, 20-F, 6-K filings."""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "companies.json"
DATA_DIR = PROJECT_ROOT / "data"

USER_AGENT = "ResearchPipeline/1.0 (ernest.limwj@gmail.com)"

# SEC rate limit: max 10 requests/second — we use 0.12s between requests
REQUEST_DELAY = 0.12

# Filing types to download
FILING_TYPES = {"10-K", "10-Q", "8-K", "20-F", "6-K"}

SUBMISSIONS_BASE = "https://data.sec.gov/submissions"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"


def _load_companies():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _get_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
    })
    return session


def _pad_cik(cik):
    """Pad CIK to 10 digits as required by the submissions API."""
    return str(cik).lstrip("0").zfill(10)


def _raw_cik(cik):
    """CIK without leading zeros, used in archive URLs."""
    return str(cik).lstrip("0")


def _fetch_submissions(session, cik):
    """Fetch company submissions from the EDGAR submissions API.
    Returns the full JSON response or None on failure."""
    padded = _pad_cik(cik)
    url = f"{SUBMISSIONS_BASE}/CIK{padded}.json"
    print(f"  Fetching submissions from: {url}")

    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        time.sleep(REQUEST_DELAY)
        return resp.json()
    except requests.RequestException as e:
        print(f"  Failed to fetch submissions: {e}")
        return None


def _parse_filings(submissions_data, since_year):
    """Extract recent filings from submissions JSON.
    Returns list of dicts with keys: form, date, accession, primary_doc."""
    filings = []
    recent = submissions_data.get("filings", {}).get("recent", {})

    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    for i in range(len(forms)):
        form = forms[i]
        if form not in FILING_TYPES:
            continue

        filing_date = dates[i] if i < len(dates) else ""
        accession = accessions[i] if i < len(accessions) else ""
        primary_doc = primary_docs[i] if i < len(primary_docs) else ""

        # Filter by year
        try:
            year = int(filing_date[:4])
        except (ValueError, IndexError):
            continue

        if year < since_year:
            continue

        filings.append({
            "form": form,
            "date": filing_date,
            "accession": accession,
            "primary_doc": primary_doc,
        })

    # Also check older filings files if they exist
    older_files = submissions_data.get("filings", {}).get("files", [])
    for file_ref in older_files:
        name = file_ref.get("name", "")
        if not name:
            continue
        # We could fetch these too, but the recent filings typically cover
        # 3+ years. Skip for now to stay within rate limits.

    return filings


def _build_filing_url(cik, accession, primary_doc):
    """Build the URL for a specific filing document."""
    raw = _raw_cik(cik)
    # Accession number format: 0000320193-24-000081 -> remove dashes for path
    accession_path = accession.replace("-", "")
    return f"{ARCHIVES_BASE}/{raw}/{accession_path}/{primary_doc}"


def _sanitize_filename(name):
    """Remove characters that are unsafe for filenames."""
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def _find_pdf_docs(session, cik, accession):
    """Fetch the filing index and return a list of PDF document filenames."""
    raw = _raw_cik(cik)
    accession_path = accession.replace("-", "")
    index_url = f"{ARCHIVES_BASE}/{raw}/{accession_path}/index.json"

    try:
        resp = session.get(index_url, timeout=30)
        resp.raise_for_status()
        time.sleep(REQUEST_DELAY)
        data = resp.json()
    except (requests.RequestException, ValueError):
        return []

    pdfs = []
    for item in data.get("directory", {}).get("item", []):
        name = item.get("name", "")
        if name.lower().endswith(".pdf"):
            pdfs.append(name)
    return pdfs


def _download_filing(url, local_path, session):
    """Download a single filing. Returns status string."""
    if local_path.exists():
        return "skipped"

    try:
        resp = session.get(url, timeout=60, stream=True)
        resp.raise_for_status()

        local_path.parent.mkdir(parents=True, exist_ok=True)
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        time.sleep(REQUEST_DELAY)
        return "downloaded"
    except requests.RequestException as e:
        return f"failed: {e}"


def _render_html_to_pdf(html_path, pdf_path):
    """Render an HTML filing to PDF using Playwright (headless Chromium).
    Returns True on success, False on failure."""
    if pdf_path.exists():
        return True

    try:
        file_url = html_path.as_uri()
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(file_url, wait_until="networkidle", timeout=60000)
            page.pdf(path=str(pdf_path), format="Letter", print_background=True)
            browser.close()
        return True
    except Exception as e:
        print(f"    Failed to render PDF: {e}")
        return False


def _ensure_pdfs_for_filings(sec_dir, session, cik, filings):
    """Ensure every 10-K and 10-Q has a PDF version.
    First checks the EDGAR index for a PDF companion. If none,
    renders the HTML filing to PDF via Playwright."""
    form_types_needing_pdf = {"10-K", "10-Q", "20-F", "6-K"}
    converted = 0
    already_had = 0

    for filing in filings:
        form = filing["form"]
        if form not in form_types_needing_pdf:
            continue

        date = filing["date"]
        accession = filing["accession"]
        safe_accession = _sanitize_filename(accession)
        form_dir = sec_dir / form

        if not form_dir.exists():
            continue

        # Check if a PDF already exists for this filing
        pdf_pattern = f"{date}_{safe_accession}"
        existing_pdfs = [f for f in form_dir.iterdir()
                         if f.suffix.lower() == ".pdf" and f.name.startswith(pdf_pattern)]
        if existing_pdfs:
            already_had += 1
            continue

        # Try to find PDF from EDGAR filing index
        pdf_docs = _find_pdf_docs(session, cik, accession)
        found_pdf = False
        for pdf_name in pdf_docs:
            pdf_url = _build_filing_url(cik, accession, pdf_name)
            pdf_filename = f"{date}_{safe_accession}_{_sanitize_filename(pdf_name)}"
            pdf_local = form_dir / pdf_filename
            status = _download_filing(pdf_url, pdf_local, session)
            if status in ("downloaded", "skipped"):
                print(f"  PDF from EDGAR: {form}/{pdf_filename}")
                found_pdf = True
                break

        if found_pdf:
            converted += 1
            continue

        # No PDF on EDGAR — render HTML to PDF via Playwright
        html_file = form_dir / f"{date}_{safe_accession}.htm"
        if not html_file.exists():
            # Try .html extension
            html_file = form_dir / f"{date}_{safe_accession}.html"
        if not html_file.exists():
            print(f"  No HTML found for {form}/{date}_{safe_accession}")
            continue

        pdf_file = form_dir / f"{date}_{safe_accession}.pdf"
        print(f"  Rendering PDF: {form}/{pdf_file.name} ...", end=" ", flush=True)
        if _render_html_to_pdf(html_file, pdf_file):
            size_mb = pdf_file.stat().st_size / (1024 * 1024)
            print(f"done ({size_mb:.1f} MB)")
            converted += 1
        else:
            print("FAILED")

    if converted or already_had:
        print(f"\n  PDFs: {already_had} already existed, {converted} new")


def _find_ir_overlaps(ticker, filings):
    """Check for likely duplicates between SEC downloads and IR downloads.
    Matches by date proximity (within 3 days) and document type heuristics."""
    ticker_dir = DATA_DIR / ticker
    classifications_path = ticker_dir / "classifications.json"

    if not classifications_path.exists():
        return {}

    with open(classifications_path) as f:
        classifications = json.load(f)

    ir_files = classifications.get("files", [])
    overlaps = {}

    # Map SEC form types to classifier document types
    form_to_doctype = {
        "10-K": ["regulatory_filing", "annual_report"],
        "10-Q": ["regulatory_filing", "earnings_release"],
        "8-K": ["press_release", "earnings_release"],
        "20-F": ["regulatory_filing", "annual_report"],
        "6-K": ["regulatory_filing", "press_release"],
    }

    for filing in filings:
        filing_date = filing["date"]
        form = filing["form"]
        matching_types = form_to_doctype.get(form, [])

        for ir_file in ir_files:
            ir_date = ir_file.get("date")
            ir_type = ir_file.get("document_type")

            if not ir_date or ir_type not in matching_types:
                continue

            # Check date proximity (within 3 days)
            try:
                sec_dt = datetime.strptime(filing_date, "%Y-%m-%d")
                ir_dt = datetime.strptime(ir_date, "%Y-%m-%d")
                if abs((sec_dt - ir_dt).days) <= 3:
                    key = f"{filing_date}_{filing['accession']}"
                    overlaps[key] = ir_file["filename"]
            except ValueError:
                continue

    return overlaps


def download_sec_filings(ticker, since_year=2020):
    """Download SEC EDGAR filings for a US-listed ticker.

    Args:
        ticker: Company ticker (must have sec_cik in companies.json).
        since_year: Only download filings from this year onward.
    """
    companies = _load_companies()

    if ticker not in companies:
        print(f"Error: ticker '{ticker}' not found in companies.json.")
        print("  Run: python main.py add-company")
        return

    company = companies[ticker]
    cik = company.get("sec_cik")
    if not cik:
        print(f"Error: no sec_cik configured for '{ticker}'.")
        return

    print(f"\n=== SEC EDGAR: {ticker} — {company['name']} ===")
    print(f"CIK: {cik}")
    print(f"Since: {since_year}")

    session = _get_session()

    # --- Step 1: Fetch submissions ---
    submissions = _fetch_submissions(session, cik)
    if submissions is None:
        print("Error: could not fetch EDGAR submissions.")
        return

    # --- Step 2: Parse filings ---
    filings = _parse_filings(submissions, since_year)
    print(f"Found {len(filings)} filing(s) matching criteria.")

    if not filings:
        print("Nothing to download.")
        return

    # Count by type
    type_counts = {}
    for f in filings:
        type_counts[f["form"]] = type_counts.get(f["form"], 0) + 1
    for form, count in sorted(type_counts.items()):
        print(f"  {form}: {count}")

    # --- Step 3: Download filings ---
    sec_dir = DATA_DIR / ticker / "sec"
    sec_dir.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    skipped = 0
    failed = 0
    log_entries = []

    for filing in filings:
        form = filing["form"]
        date = filing["date"]
        accession = filing["accession"]
        primary_doc = filing["primary_doc"]

        if not primary_doc:
            log_entries.append({
                "form": form,
                "date": date,
                "accession": accession,
                "status": "skipped: no primary document",
            })
            continue

        url = _build_filing_url(cik, accession, primary_doc)

        # Build local path: sec/{form_type}/{date}_{accession}.{ext}
        ext = Path(primary_doc).suffix or ".html"
        safe_accession = _sanitize_filename(accession)
        filename = f"{date}_{safe_accession}{ext}"
        form_dir = sec_dir / form
        form_dir.mkdir(parents=True, exist_ok=True)
        local_path = form_dir / filename

        status = _download_filing(url, local_path, session)

        safe_name = filename.encode("ascii", errors="replace").decode()
        if status == "downloaded":
            print(f"  Downloading: {form}/{safe_name}")
            downloaded += 1
        elif status == "skipped":
            skipped += 1
        else:
            print(f"  FAILED: {form}/{safe_name} — {status}")
            failed += 1

        log_entries.append({
            "form": form,
            "date": date,
            "accession": accession,
            "url": url,
            "local_path": str(local_path),
            "filename": filename,
            "status": status,
        })

        # Also look for PDF versions of the filing
        pdf_docs = _find_pdf_docs(session, cik, accession)
        for pdf_name in pdf_docs:
            pdf_url = _build_filing_url(cik, accession, pdf_name)
            pdf_filename = f"{date}_{safe_accession}_{_sanitize_filename(pdf_name)}"
            pdf_local = form_dir / pdf_filename

            pdf_status = _download_filing(pdf_url, pdf_local, session)

            pdf_safe = pdf_filename.encode("ascii", errors="replace").decode()
            if pdf_status == "downloaded":
                print(f"  Downloading: {form}/{pdf_safe}")
                downloaded += 1
            elif pdf_status == "skipped":
                skipped += 1
            else:
                print(f"  FAILED: {form}/{pdf_safe} — {pdf_status}")
                failed += 1

            log_entries.append({
                "form": form,
                "date": date,
                "accession": accession,
                "url": pdf_url,
                "local_path": str(pdf_local),
                "filename": pdf_filename,
                "status": pdf_status,
                "is_pdf_companion": True,
            })

    # --- Step 4: Ensure PDFs exist for 10-K/10-Q ---
    print("\nEnsuring PDF versions exist for 10-K/10-Q filings...")
    _ensure_pdfs_for_filings(sec_dir, session, cik, filings)

    # --- Step 5: Check for IR overlaps (renumbered) ---
    overlaps = _find_ir_overlaps(ticker, filings)

    if overlaps:
        print(f"\n  Possible IR overlaps ({len(overlaps)}):")
        for sec_key, ir_file in overlaps.items():
            ir_safe = ir_file.encode("ascii", errors="replace").decode()
            print(f"    SEC {sec_key} <-> IR {ir_safe}")

    # --- Step 6: Summary ---
    print(f"\n=== SEC EDGAR Summary ===")
    print(f"Downloaded {downloaded} new filing(s).")
    if skipped:
        print(f"{skipped} filing(s) already existed (skipped).")
    if failed:
        print(f"{failed} filing(s) failed.")

    # --- Step 7: Write download log ---
    log_path = sec_dir / "download_log.json"

    # Merge with existing log
    existing_log = {}
    if log_path.exists():
        with open(log_path) as f:
            existing_log = json.load(f)

    log_data = {
        "ticker": ticker,
        "company": company["name"],
        "cik": cik,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "since_year": since_year,
        "files": log_entries,
        "ir_overlaps": overlaps,
        "summary": {
            "downloaded": downloaded,
            "skipped": skipped,
            "failed": failed,
            "total": len(filings),
        },
    }
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log_data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Download log saved to: {log_path}")


_MONTH_TO_NUM = {
    "January": 1, "February": 2, "March": 3, "April": 4,
    "May": 5, "June": 6, "July": 7, "August": 8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}


def _filing_to_fiscal_period(filing_date_str, fy_end_month, form_type):
    """Determine fiscal period from filing date and fiscal year-end month.

    Returns (label, fiscal_year) where label is e.g. "FY" or "1Q".

    Logic:
    - For 10-K: filed 60-90 days after FY end. FY year = calendar year of
      most recent FY end before the filing date.
    - For 10-Q: estimate period end as ~2 months before filing date, then
      map to the fiscal quarter.
    """
    filing_year = int(filing_date_str[:4])
    filing_month = int(filing_date_str[5:7])

    if form_type == "10-K":
        # 10-K is filed after FY end; determine which FY
        if filing_month > fy_end_month:
            fy = filing_year
        else:
            fy = filing_year - 1
        return "FY", fy

    # 10-Q: estimate the period end month (~35-60 days before filing)
    est_month = filing_month - 2
    if est_month <= 0:
        est_month += 12
        est_year = filing_year - 1
    else:
        est_year = filing_year

    # Map to fiscal quarter based on offset from FY end
    offset = (est_month - fy_end_month) % 12
    if offset == 0:
        quarter = 4
    elif offset <= 3:
        quarter = 1
    elif offset <= 6:
        quarter = 2
    else:
        quarter = 3

    # Fiscal year = calendar year in which the FY ends
    if est_month > fy_end_month:
        fy = est_year + 1
    else:
        fy = est_year

    return f"{quarter}Q", fy


def organize_filings(ticker):
    """Create a clean filings/ folder with human-readable PDF names.

    Uses the company's fiscal_year_end to determine correct fiscal quarters.
    Naming convention:
        10-K: {TICKER}_10-K_FY{YY}.pdf
        10-Q: {TICKER}_10-Q_{N}QFY{YY}.pdf  (e.g. LITE_10-Q_1QFY26.pdf)
    """
    companies = _load_companies()
    company = companies.get(ticker, {})
    fy_end_name = company.get("fiscal_year_end", "December")
    fy_end_month = _MONTH_TO_NUM.get(fy_end_name, 12)

    ticker_dir = DATA_DIR / ticker
    sec_dir = ticker_dir / "sec"
    filings_dir = ticker_dir / "filings"

    if not sec_dir.exists():
        print(f"No SEC filings found for {ticker}.")
        return

    print(f"=== Organizing filings: {ticker} (FY ends {fy_end_name}) ===")

    # Clean out old filings folder to avoid stale files
    import shutil
    if filings_dir.exists():
        shutil.rmtree(filings_dir)

    index = []
    # 20-F is the foreign equivalent of 10-K, 6-K is the foreign equivalent of 10-Q
    for form_type in ("10-K", "20-F", "10-Q", "6-K"):
        form_dir = sec_dir / form_type
        if not form_dir.exists():
            continue

        # 20-F goes into 10-K output folder, 6-K goes into 10-Q
        if form_type == "20-F":
            out_dir = filings_dir / "10-K"
        elif form_type == "6-K":
            out_dir = filings_dir / "10-Q"
        else:
            out_dir = filings_dir / form_type
        out_dir.mkdir(parents=True, exist_ok=True)

        # Find all PDFs for this form type
        pdfs = sorted(
            [f for f in form_dir.iterdir() if f.suffix.lower() == ".pdf"],
            key=lambda f: f.name,
            reverse=True,  # newest first
        )

        # For fiscal period mapping, treat 20-F like 10-K, 6-K like 10-Q
        if form_type == "20-F":
            effective_type = "10-K"
        elif form_type == "6-K":
            effective_type = "10-Q"
        else:
            effective_type = form_type

        # Compute fiscal period and clean name for each PDF
        pdf_entries = []
        for pdf in pdfs:
            date_match = re.match(r"(\d{4}-\d{2}-\d{2})_", pdf.name)
            filing_date = date_match.group(1) if date_match else None

            if filing_date:
                label, fy = _filing_to_fiscal_period(
                    filing_date, fy_end_month, effective_type)
                fy_short = f"{fy % 100:02d}"
                if label == "FY":
                    period = f"FY{fy_short}"
                    clean_name = f"{ticker}_10-K_FY{fy_short}.pdf"
                else:
                    period = f"{label}FY{fy_short}"
                    clean_name = f"{ticker}_10-Q_{period}.pdf"
            else:
                period = None
                clean_name = f"{ticker}_{form_type}_{pdf.stem}.pdf"

            pdf_entries.append({
                "pdf": pdf,
                "filing_date": filing_date,
                "period": period,
                "clean_name": clean_name,
            })

        # Detect collisions (shouldn't happen with correct fiscal mapping,
        # but handle gracefully by appending filing date)
        name_counts = {}
        for pe in pdf_entries:
            name_counts[pe["clean_name"]] = name_counts.get(pe["clean_name"], 0) + 1
        for pe in pdf_entries:
            if name_counts[pe["clean_name"]] > 1 and pe["filing_date"]:
                base = pe["clean_name"].replace(".pdf", "")
                pe["clean_name"] = f"{base}_{pe['filing_date']}.pdf"

        # Copy files
        for pe in pdf_entries:
            pdf = pe["pdf"]
            clean_name = pe["clean_name"]
            filing_date = pe["filing_date"]
            period = pe["period"]
            dest = out_dir / clean_name
            shutil.copy2(pdf, dest)

            entry = {
                "filename": clean_name,
                "form_type": form_type,
                "filing_date": filing_date,
                "period": period,
                "path": str(dest.relative_to(ticker_dir)),
                "source": str(pdf.relative_to(ticker_dir)),
                "size_kb": round(dest.stat().st_size / 1024),
            }
            index.append(entry)
            print(f"  {clean_name} ({entry['size_kb']} KB)")

    # Write index
    index_path = filings_dir / "index.json"
    index_data = {
        "ticker": ticker,
        "fiscal_year_end": fy_end_name,
        "organized_at": datetime.now(timezone.utc).isoformat(),
        "filings": index,
        "summary": {
            "10-K": len([e for e in index if e["form_type"] == "10-K"]),
            "10-Q": len([e for e in index if e["form_type"] == "10-Q"]),
            "total": len(index),
        },
    }
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index_data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"\n=== Organized {len(index)} filing(s) into {filings_dir} ===")
    print(f"Index saved to: {index_path}")
