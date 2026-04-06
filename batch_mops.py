"""Batch MOPS download for all TW companies missing filings."""
import sys
import os
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

import json

with open('config/companies.json', encoding='utf-8') as f:
    companies = json.load(f)

# Find all TW companies that need MOPS
tickers = []
for ticker, info in sorted(companies.items()):
    if info.get('market') != 'TW':
        continue
    filings_dir = Path('data') / ticker / 'filings'
    filings = list(filings_dir.rglob('*.pdf')) if filings_dir.exists() else []
    if len(filings) < 10:  # needs more coverage
        tickers.append(ticker)

print(f"MOPS batch: {len(tickers)} TW companies since 2020\n")

from scripts.mops import download_mops_filings

for i, ticker in enumerate(tickers, 1):
    print(f"\n[{i}/{len(tickers)}] {ticker} — {companies[ticker]['name']}")
    try:
        download_mops_filings(ticker, since_year=2020)
    except Exception as e:
        print(f"  ERROR: {str(e)[:100]}")

print(f"\n{'='*60}")
print(f"MOPS BATCH COMPLETE: {len(tickers)} companies")
print(f"{'='*60}")
