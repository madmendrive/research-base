"""Wave 6: US companies — IR scrape parallel + SEC EDGAR."""
import sys
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

# New US companies + existing ones that need SEC EDGAR
tickers = [
    "ADEA", "AMAT", "AMZN", "ANET", "ARM", "AVGO", "CIEN", "COHR",
    "CRDO", "DELL", "FORM", "GFS", "GLW", "HPE", "HPQ", "IBM",
    "INTC", "LRCX", "MU", "NVDA", "ORCL", "SMCI", "SNPS", "STX",
    "SWKS", "TER", "TSLA", "VRT", "WDC",
    # Also catch up existing US companies
    "AAPL", "AMD", "AMKR", "ASML", "KLAC", "LITE",
]
since_year = 2020


def phase1_ir(ticker):
    from scripts.ir_scraper import scrape_ir
    try:
        scrape_ir(ticker, limit=200, since_year=since_year)
    except Exception as e:
        print(f"  [{ticker}] IR error: {str(e)[:80]}")
    return ticker


def phase2_sec(ticker):
    """SEC EDGAR download + classify + organize."""
    from scripts.sec_edgar import download_sec_filings, organize_filings
    from scripts.classifier import classify_documents
    import json
    with open('config/companies.json', encoding='utf-8') as f:
        companies = json.load(f)
    company = companies.get(ticker, {})
    if not company.get('sec_cik'):
        print(f"  [{ticker}] No SEC CIK, skipping")
        return ticker
    try:
        download_sec_filings(ticker, since_year=since_year)
    except Exception as e:
        print(f"  [{ticker}] SEC error: {str(e)[:80]}")
    try:
        classify_documents(ticker)
    except Exception as e:
        print(f"  [{ticker}] Classify error: {str(e)[:80]}")
    try:
        organize_filings(ticker)
    except Exception as e:
        print(f"  [{ticker}] Organize error: {str(e)[:80]}")
    return ticker


if __name__ == "__main__":
    print(f"WAVE 6 (US): {len(tickers)} companies since {since_year}\n")

    # Phase 1: IR scrape in parallel
    print("PHASE 1: IR scraping (8 parallel workers)...\n")
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(phase1_ir, t): t for t in tickers}
        done = 0
        for f in as_completed(futures):
            done += 1
            print(f"  IR done: {futures[f]} ({done}/{len(tickers)})")

    # Phase 2: SEC EDGAR (sequential — respects SEC rate limits)
    print(f"\nPHASE 2: SEC EDGAR downloads...\n")
    for i, t in enumerate(tickers, 1):
        print(f"\n[{i}/{len(tickers)}] {t}")
        try:
            phase2_sec(t)
        except Exception as e:
            print(f"  Error: {str(e)[:100]}")

    print(f"\n{'='*60}")
    print(f"WAVE 6 COMPLETE: {len(tickers)} US companies")
    print(f"{'='*60}")
