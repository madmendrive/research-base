"""Batch download materials for multiple tickers — sequential, one at a time."""
import sys
import os
import time

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

tickers = [
    "6857 JT",  # Advantest
    "6770 JT",  # Alps Alpine
    "6754 JT",  # Anritsu
    "6407 JT",  # CKD
    "6383 JT",  # Daifuku
    "4980 JT",  # Dexerials
    "6146 JT",  # Disco
    "6954 JT",  # Fanuc
    "285A JT",  # Kioxia
    "6526 JT",  # Socionext
    "6920 JT",  # Lasertec
    "9880 JT",  # Innotech
    "5344 JT",  # MARUWA
    "3110 JT",  # Nitto Boseki
]

since_year = 2020

for i, ticker in enumerate(tickers, 1):
    print(f"\n{'='*60}")
    print(f"[{i}/{len(tickers)}] {ticker}")
    print(f"{'='*60}")
    try:
        # IR scraper
        from scripts.ir_scraper import scrape_ir
        scrape_ir(ticker, limit=200, since_year=since_year)

        # Classify
        from scripts.classifier import classify_documents
        classify_documents(ticker)

        # Organize IR filings
        from scripts.filing_gaps import organize_ir_filings, analyze_gaps, print_gap_report
        organize_ir_filings(ticker)

        # Gap analysis
        gaps = analyze_gaps(ticker, since_year)
        has_gaps = print_gap_report(ticker, gaps)

        if has_gaps:
            # EDINET for gaps
            from scripts.edinet import download_edinet_filings
            download_edinet_filings(ticker, since_year=since_year)

            # Re-check
            gaps = analyze_gaps(ticker, since_year)
            if gaps["missing_annual"] or gaps["missing_quarterly"]:
                from scripts.tdnet import download_tdnet_filings
                download_tdnet_filings(ticker, since_year=since_year)

    except Exception as e:
        print(f"  ERROR: {str(e)[:150]}")

    # Brief pause between tickers
    time.sleep(2)

print(f"\n{'='*60}")
print(f"BATCH COMPLETE: {len(tickers)} tickers processed")
print(f"{'='*60}")
