"""Batch download monthly revenue for all TW companies."""
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

tickers = [(t, i) for t, i in sorted(companies.items()) if i.get('market') == 'TW']
print(f"Monthly revenue download: {len(tickers)} TW companies since 2020\n")

from playwright.sync_api import sync_playwright
from scripts.mops import _download_monthly_revenue

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(
        ignore_https_errors=True,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    )

    for i, (ticker, info) in enumerate(tickers, 1):
        stock_code = info.get('twse_code') or ticker.split()[0]
        mops_dir = Path('data') / ticker / 'mops'
        mops_dir.mkdir(parents=True, exist_ok=True)

        # Check if already has monthly revenue
        rev_dir = mops_dir / 'monthly_revenue'
        existing = len(list(rev_dir.glob('*.pdf'))) if rev_dir.exists() else 0
        if existing >= 40:  # already has most months
            print(f"[{i}/{len(tickers)}] {ticker} — already has {existing} monthly PDFs, skipping")
            continue

        print(f"[{i}/{len(tickers)}] {ticker} — {info['name']}")
        try:
            _download_monthly_revenue(ctx, stock_code, mops_dir, 2020)
        except Exception as e:
            print(f"  ERROR: {str(e)[:80]}")

    browser.close()

print(f"\n{'='*60}")
print("MONTHLY REVENUE BATCH COMPLETE")
print(f"{'='*60}")
