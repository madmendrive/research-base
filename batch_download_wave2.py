"""Batch download wave 2: 19 more Japanese companies."""
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
    "7760 JT", "4975 JT", "6861 JT", "6525 JT", "6323 JT",
    "6723 JT", "268A JT", "6777 JT", "7735 JT", "6834 JT",
    "4063 JT", "3436 JT", "6976 JT", "6762 JT", "6481 JT",
    "8035 JT", "7729 JT", "4704 JT", "6506 JT",
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


def phase2_gaps(ticker):
    from scripts.filing_gaps import analyze_gaps, print_gap_report
    gaps = analyze_gaps(ticker, since_year)
    has_gaps = print_gap_report(ticker, gaps)
    return ticker, has_gaps


def phase3_edinet(ticker):
    from scripts.edinet import download_edinet_filings
    from scripts.filing_gaps import analyze_gaps, print_gap_report
    download_edinet_filings(ticker, since_year=since_year)
    gaps = analyze_gaps(ticker, since_year)
    print_gap_report(ticker, gaps)
    return ticker


if __name__ == "__main__":
    print(f"{'='*60}")
    print(f"WAVE 2: {len(tickers)} companies since {since_year}")
    print(f"{'='*60}\n")

    # Phase 1: parallel IR
    print("PHASE 1: IR scraping (8 parallel workers)...\n")
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(phase1_ir, t): t for t in tickers}
        done = 0
        for f in as_completed(futures):
            done += 1
            print(f"  IR done: {futures[f]} ({done}/{len(tickers)})")

    # Phase 2: gap analysis
    print(f"\nPHASE 2: Gap analysis...\n")
    need_edinet = []
    for t in tickers:
        _, has_gaps = phase2_gaps(t)
        if has_gaps:
            need_edinet.append(t)

    print(f"\n  Need EDINET: {len(need_edinet)}/{len(tickers)}: {need_edinet}")

    # Phase 3: EDINET for gaps only
    if need_edinet:
        print(f"\nPHASE 3: EDINET for {len(need_edinet)} companies...\n")
        for i, t in enumerate(need_edinet, 1):
            print(f"[{i}/{len(need_edinet)}] {t}")
            try:
                phase3_edinet(t)
            except Exception as e:
                print(f"  Error: {str(e)[:100]}")
            time.sleep(1)

    print(f"\n{'='*60}")
    print(f"WAVE 2 COMPLETE")
    print(f"{'='*60}")
