"""Wave 5: 12 more TW companies — IR scrape parallel + MOPS for all."""
import sys
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

tickers = [
    "1802 TT", "6274 TT", "2303 TT", "3037 TT", "5347 TT",
    "1605 TT", "3105 TT", "2344 TT", "3231 TT", "6669 TT",
    "2327 TT", "4958 TT",
]
since_year = 2020


def phase1_ir(ticker):
    from scripts.ir_scraper import scrape_ir
    from scripts.classifier import classify_documents
    from scripts.filing_gaps import organize_ir_filings
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
    return ticker


def phase2_mops(ticker):
    from scripts.mops import download_mops_filings
    from scripts.filing_gaps import analyze_gaps, print_gap_report
    try:
        download_mops_filings(ticker, since_year=since_year)
    except Exception as e:
        print(f"  [{ticker}] MOPS error: {str(e)[:80]}")
    try:
        gaps = analyze_gaps(ticker, since_year)
        print_gap_report(ticker, gaps)
    except Exception:
        pass
    return ticker


if __name__ == "__main__":
    print(f"WAVE 5: {len(tickers)} TW companies since {since_year}\n")

    print("PHASE 1: IR scraping (8 parallel workers)...\n")
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(phase1_ir, t): t for t in tickers}
        done = 0
        for f in as_completed(futures):
            done += 1
            print(f"  IR done: {futures[f]} ({done}/{len(tickers)})")

    print(f"\nPHASE 2: MOPS downloads (sequential)...\n")
    for i, t in enumerate(tickers, 1):
        print(f"[{i}/{len(tickers)}] {t}")
        phase2_mops(t)

    print(f"\n{'='*60}")
    print(f"WAVE 5 COMPLETE")
    print(f"{'='*60}")
