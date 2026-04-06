"""Filing gap analysis — determines which fiscal periods are covered by
IR-scraped documents and which need to be fetched from regulatory sources."""

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_PATH = PROJECT_ROOT / "config" / "companies.json"

_MONTH_TO_NUM = {
    "January": 1, "February": 2, "March": 3, "April": 4,
    "May": 5, "June": 6, "July": 7, "August": 8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}

# Classification types that map to filings
ANNUAL_TYPES = {"annual_report", "regulatory_filing"}
QUARTERLY_TYPES = {"regulatory_filing", "quarterly_presentation", "earnings_release",
                   "financial_supplement"}


def _load_companies():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _parse_period(period_str):
    """Parse a classification period string into (type, year, quarter).

    Examples:
        'FY2024' → ('FY', 2024, None)
        'Q1 2024' → ('Q', 2024, 1)
        'H1 2024' → ('H', 2024, 1)
    Returns None if unparseable.
    """
    if not period_str:
        return None

    # FY2024
    m = re.match(r'FY\s*(\d{4})', period_str)
    if m:
        return ('FY', int(m.group(1)), None)

    # Q1 2024 or Q1FY24
    m = re.match(r'Q(\d)\s*(\d{4})', period_str)
    if m:
        return ('Q', int(m.group(2)), int(m.group(1)))

    # H1 2024
    m = re.match(r'H(\d)\s*(\d{4})', period_str)
    if m:
        return ('H', int(m.group(2)), int(m.group(1)))

    return None


def _expected_periods(since_year, fy_end_month, market="US"):
    """Generate the list of expected fiscal periods from since_year to now.

    Returns dict with keys 'annual' and 'quarterly', each a list of
    (label, year) tuples. For December FY-end:
        annual: [('FY', 2020), ('FY', 2021), ...]
        quarterly: [('1Q', 2020), ('2Q', 2020), ('3Q', 2020), ('1Q', 2021), ...]

    Note: Japan abolished mandatory quarterly securities reports (四半期報告書) from
    April 2024, but companies still publish quarterly earnings (決算短信) and
    presentations voluntarily. We still expect Q1/Q2/Q3 coverage for all markets —
    the difference is just the *source* (EDINET won't have them, but TDNet/IR will).
    """
    current_year = datetime.now().year
    current_month = datetime.now().month

    annuals = []
    quarterlies = []

    for year in range(since_year, current_year + 1):
        # Has this FY ended yet?
        if fy_end_month == 12:
            fy_ended = year < current_year or (year == current_year and current_month > 3)
        else:
            # For non-Dec FY (e.g. March), FY2024 ends March 2025
            fy_end_cal_year = year + 1 if fy_end_month <= 6 else year
            fy_ended = (fy_end_cal_year < current_year or
                        (fy_end_cal_year == current_year and current_month > fy_end_month + 3))

        if fy_ended:
            annuals.append(("FY", year))

        # Standard quarterly: Q1, Q2, Q3 (Q4 is covered by annual)
        for q in (1, 2, 3):
            # Determine calendar month of quarter end
            if fy_end_month == 12:
                q_end_month = q * 3
                q_end_year = year
            else:
                q_end_month = (fy_end_month + q * 3) % 12
                if q_end_month == 0:
                    q_end_month = 12
                q_end_year = year if (fy_end_month + q * 3) <= 12 else year + 1

            # Has this quarter ended + time for filing (~2 months)?
            q_ended = (q_end_year < current_year or
                       (q_end_year == current_year and current_month > q_end_month + 2))

            if q_ended:
                quarterlies.append((f"{q}Q", year))

    return {"annual": annuals, "quarterly": quarterlies}


def organize_ir_filings(ticker):
    """Organize classified IR files into data/{ticker}/filings/.

    Reads classifications.json and copies annual_report/regulatory_filing
    documents into the filings/ structure with fiscal period naming.
    """
    companies = _load_companies()
    company = companies.get(ticker, {})
    fy_end_name = company.get("fiscal_year_end", "December")

    ticker_dir = DATA_DIR / ticker
    class_path = ticker_dir / "classifications.json"
    if not class_path.exists():
        return

    with open(class_path, encoding="utf-8") as f:
        data = json.load(f)
    files = data.get("files", [])

    filings_dir = ticker_dir / "filings"
    organized = 0

    for entry in files:
        doc_type = entry.get("document_type", "")
        period = entry.get("period")
        source_dir = entry.get("source_dir", "")
        filename = entry.get("filename", "")

        if not period or doc_type not in (ANNUAL_TYPES | QUARTERLY_TYPES):
            continue

        parsed = _parse_period(period)
        if not parsed:
            continue

        p_type, year, quarter = parsed
        fy_short = f"{year % 100:02d}"
        src_path = ticker_dir / source_dir / filename
        if not src_path.exists():
            continue

        if p_type == 'FY' and doc_type in ANNUAL_TYPES:
            out_dir = filings_dir / "Annual"
            clean_name = f"{ticker}_Annual_FY{fy_short}.pdf"
        elif p_type == 'Q' and quarter:
            out_dir = filings_dir / "Quarterly"
            clean_name = f"{ticker}_Quarterly_{quarter}QFY{fy_short}.pdf"
        elif p_type == 'H':
            out_dir = filings_dir / "SemiAnnual"
            clean_name = f"{ticker}_SemiAnnual_H{quarter}FY{fy_short}.pdf"
        else:
            continue

        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / clean_name

        if not dest.exists():
            shutil.copy2(src_path, dest)
            organized += 1

    return organized


def analyze_gaps(ticker, since_year):
    """Analyze what filings exist and what's missing.

    Returns dict with 'found', 'missing_annual', 'missing_quarterly'.
    """
    companies = _load_companies()
    company = companies.get(ticker, {})
    fy_end_name = company.get("fiscal_year_end", "December")
    fy_end_month = _MONTH_TO_NUM.get(fy_end_name, 12)

    filings_dir = DATA_DIR / ticker / "filings"

    # Scan what we have
    found_annual = set()
    found_quarterly = set()

    if filings_dir.exists():
        for pdf in filings_dir.rglob("*.pdf"):
            name = pdf.name
            # Parse: {ticker}_Annual_FY{YY}.pdf
            m = re.search(r'_Annual_FY(\d{2})\.pdf', name)
            if m:
                fy = int(m.group(1))
                year = 2000 + fy if fy < 90 else 1900 + fy
                found_annual.add(("FY", year))
                continue

            # {ticker}_Quarterly_{N}QFY{YY}.pdf
            m = re.search(r'_Quarterly_(\d)QFY(\d{2})\.pdf', name)
            if m:
                q = int(m.group(1))
                fy = int(m.group(2))
                year = 2000 + fy if fy < 90 else 1900 + fy
                found_quarterly.add((f"{q}Q", year))
                continue

            # Tanshin counts as quarterly coverage too
            m = re.search(r'_Tanshin_FY(\d{2})\.pdf', name)
            if m:
                fy = int(m.group(1))
                year = 2000 + fy if fy < 90 else 1900 + fy
                found_annual.add(("FY", year))
                continue

            m = re.search(r'_Tanshin_(\d)QFY(\d{2})\.pdf', name)
            if m:
                q = int(m.group(1))
                fy = int(m.group(2))
                year = 2000 + fy if fy < 90 else 1900 + fy
                found_quarterly.add((f"{q}Q", year))
                continue

            # SemiAnnual — H1 covers Q2, H2 would cover Q4
            m = re.search(r'_SemiAnnual_H(\d)FY(\d{2})\.pdf', name)
            if m:
                h = int(m.group(1))
                fy = int(m.group(2))
                year = 2000 + fy if fy < 90 else 1900 + fy
                found_quarterly.add((f"{h * 2}Q", year))
                continue

    market = company.get("market", "US")
    expected = _expected_periods(since_year, fy_end_month, market=market)
    missing_annual = [p for p in expected["annual"] if p not in found_annual]
    missing_quarterly = [p for p in expected["quarterly"] if p not in found_quarterly]

    return {
        "found_annual": sorted(found_annual, key=lambda x: x[1]),
        "found_quarterly": sorted(found_quarterly, key=lambda x: (x[1], x[0])),
        "missing_annual": missing_annual,
        "missing_quarterly": missing_quarterly,
    }


def print_gap_report(ticker, gaps):
    """Print a human-readable gap report."""
    def _fmt(periods):
        return ", ".join(f"{p[0]}{p[1] % 100:02d}" for p in periods) or "none"

    print(f"\n=== Filing coverage: {ticker} ===")
    print(f"  Annual found:    {_fmt(gaps['found_annual'])}")
    print(f"  Quarterly found: {_fmt(gaps['found_quarterly'])}")

    if gaps["missing_annual"] or gaps["missing_quarterly"]:
        print(f"  Missing annual:    {_fmt(gaps['missing_annual'])}")
        print(f"  Missing quarterly: {_fmt(gaps['missing_quarterly'])}")
    else:
        print(f"  No gaps — all expected filings present.")

    return bool(gaps["missing_annual"] or gaps["missing_quarterly"])
