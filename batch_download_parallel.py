"""Batch download: Phase 1 = IR scrape all in parallel, Phase 2 = EDINET only for gaps."""
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
    "6857 JT", "6770 JT", "6754 JT", "6407 JT", "6383 JT", "4980 JT",
    "6146 JT", "6954 JT", "285A JT", "6526 JT", "6920 JT", "9880 JT",
    "5344 JT", "3110 JT",
]
since_year = 2020


def phase1_ir(ticker):
    """Fast: IR scrape + classify + organize."""
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
    """Check gaps, return (ticker, has_gaps, gap_info)."""
    from scripts.filing_gaps import analyze_gaps, print_gap_report
    gaps = analyze_gaps(ticker, since_year)
    has_gaps = print_gap_report(ticker, gaps)
    return ticker, has_gaps, gaps


def phase3_edinet(ticker):
    """Slow: EDINET scan for one ticker."""
    from scripts.edinet import download_edinet_filings
    from scripts.filing_gaps import analyze_gaps, print_gap_report
    download_edinet_filings(ticker, since_year=since_year)
    gaps = analyze_gaps(ticker, since_year)
    print_gap_report(ticker, gaps)
    return ticker


if __name__ == "__main__":
    # === Phase 1: IR scrape all in parallel ===
    print(f"{'='*60}")
    print(f"PHASE 1: IR scraping {len(tickers)} companies in parallel (8 workers)")
    print(f"{'='*60}\n")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(phase1_ir, t): t for t in tickers}
        done = 0
        for future in as_completed(futures):
            done += 1
            ticker = futures[future]
            try:
                future.result()
                print(f"  Phase 1 done: {ticker} ({done}/{len(tickers)})")
            except Exception as e:
                print(f"  Phase 1 FAIL: {ticker}: {e}")

    # === Phase 2: Gap analysis (fast, sequential) ===
    print(f"\n{'='*60}")
    print(f"PHASE 2: Gap analysis")
    print(f"{'='*60}\n")

    need_edinet = []
    no_gaps = []
    for ticker in tickers:
        ticker, has_gaps, gaps = phase2_gaps(ticker)
        if has_gaps:
            need_edinet.append(ticker)
        else:
            no_gaps.append(ticker)

    print(f"\n  No gaps: {len(no_gaps)} companies: {no_gaps}")
    print(f"  Need EDINET: {len(need_edinet)} companies: {need_edinet}")

    # === Phase 3: EDINET only for companies with gaps (sequential — API rate limit) ===
    if need_edinet:
        print(f"\n{'='*60}")
        print(f"PHASE 3: EDINET scan for {len(need_edinet)} companies with gaps")
        print(f"{'='*60}\n")

        for i, ticker in enumerate(need_edinet, 1):
            print(f"\n[{i}/{len(need_edinet)}] EDINET: {ticker}")
            try:
                phase3_edinet(ticker)
            except Exception as e:
                print(f"  EDINET error: {str(e)[:100]}")
            time.sleep(1)

    print(f"\n{'='*60}")
    print(f"BATCH COMPLETE")
    print(f"{'='*60}")
