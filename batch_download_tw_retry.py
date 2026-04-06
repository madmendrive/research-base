"""Retry TW companies with corrected IR URLs — parallel, deep mode."""
import sys
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

tickers = [
    "1303 TT", "1590 TT", "2308 TT", "2337 TT", "2353 TT",
    "2360 TT", "2395 TT", "2408 TT", "2409 TT", "2474 TT",
    "3006 TT", "3008 TT", "3131 TT", "3163 TT", "3189 TT",
    "3406 TT", "3481 TT", "3665 TT", "4979 TT", "5269 TT",
    "6187 TT", "6442 TT", "6488 TT", "6510 TT", "7769 TT",
    "8046 TT",
]
since_year = 2020


def process(ticker):
    from scripts.ir_scraper import scrape_ir
    from scripts.classifier import classify_documents
    from scripts.filing_gaps import organize_ir_filings, analyze_gaps, print_gap_report
    try:
        scrape_ir(ticker, limit=200, since_year=since_year, deep=True)
    except Exception as e:
        print(f"  [{ticker}] IR error: {str(e)[:80]}")
    try:
        classify_documents(ticker)
    except Exception as e:
        print(f"  [{ticker}] Classify error: {str(e)[:80]}")
    try:
        organize_ir_filings(ticker)
    except Exception:
        pass
    try:
        gaps = analyze_gaps(ticker, since_year)
        print_gap_report(ticker, gaps)
    except Exception:
        pass
    return ticker


if __name__ == "__main__":
    print(f"TW RETRY: {len(tickers)} companies with corrected URLs (deep mode)")
    print(f"Parallel (6 workers)...\n")

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(process, t): t for t in tickers}
        done = 0
        for f in as_completed(futures):
            done += 1
            t = futures[f]
            try:
                f.result()
                print(f"\n  Done: {t} ({done}/{len(tickers)})")
            except Exception as e:
                print(f"\n  FAIL: {t}: {e}")

    print(f"\n{'='*60}")
    print(f"TW RETRY COMPLETE")
    print(f"{'='*60}")
