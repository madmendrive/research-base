"""Batch download wave 3: Korean companies via DART."""
import sys
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

tickers = [
    "002790 KS", "000150 KS", "012510 KS", "030520 KS", "012450 KS",
    "008770 KS", "007660 KS", "035720 KS", "051900 KS", "011070 KS",
    "079550 KS", "035420 KS", "036570 KS", "009150 KS", "005930 KS",
    "018260 KS", "000660 KS",
]
since_year = 2020


def phase1_ir(ticker):
    from scripts.ir_scraper import scrape_ir
    from scripts.classifier import classify_documents
    from scripts.filing_gaps import organize_ir_filings
    try:
        scrape_ir(ticker, limit=200, since_year=since_year)
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


def phase2_dart(ticker):
    """DART download for Korean companies."""
    from scripts.dart import download_dart_filings
    from scripts.filing_gaps import analyze_gaps, print_gap_report
    try:
        download_dart_filings(ticker, since_year=since_year)
    except Exception as e:
        print(f"  [{ticker}] DART error: {str(e)[:80]}")
    gaps = analyze_gaps(ticker, since_year)
    print_gap_report(ticker, gaps)
    return ticker


if __name__ == "__main__":
    print(f"{'='*60}")
    print(f"WAVE 3 (KOREA): {len(tickers)} companies since {since_year}")
    print(f"{'='*60}\n")

    # Phase 1: parallel IR
    print("PHASE 1: IR scraping (8 parallel workers)...\n")
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(phase1_ir, t): t for t in tickers}
        done = 0
        for f in as_completed(futures):
            done += 1
            print(f"  IR done: {futures[f]} ({done}/{len(tickers)})")

    # Phase 2: DART for all (DART is fast — API-based, no date scanning)
    print(f"\nPHASE 2: DART downloads...\n")
    for i, t in enumerate(tickers, 1):
        print(f"[{i}/{len(tickers)}] {t}")
        try:
            phase2_dart(t)
        except Exception as e:
            print(f"  Error: {str(e)[:100]}")
        time.sleep(1)

    print(f"\n{'='*60}")
    print(f"WAVE 3 COMPLETE")
    print(f"{'='*60}")
