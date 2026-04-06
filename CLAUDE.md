# Research Pipeline — Project State

## What This Is

An equity research automation platform built for a buy-side analyst at a macro hedge fund running equity long/short. The system downloads, stores, classifies, extracts, and analyses financial filings and research across 140 companies in 4 markets (US, Japan, Taiwan, Korea), plus macro research by 15 authors, and thematic/sector research.

## Architecture

### CLI (`main.py`)
20 Click commands. Key ones:
- `download-materials {TICKER}` — IR scrape → classify → gap analysis → regulatory downloader (SEC/EDINET/DART/MOPS)
- `store-research {TICKER} file1.pdf [file2.pdf]` — Store sellside research with Claude extraction + analysis report
- `store-macro --file path --author "Name"` — Store macro research with view history tracking
- `store-thematic "THEME" file1.pdf` — Store thematic/sector research
- `analyse`, `analyse-macro`, `analyse-thematic` — Comparative analysis against stored research base
- `show-summary`, `show-macro-summary`, `show-author-history` — Display research summaries
- `gui` — Launch Flask web dashboard

### Flask Web GUI (`app.py`)
Runs on localhost (auto-finds free port 5080-5099). Features:
- **Morning/Evening Brief** — Claude-generated news digest, structured by World/Asia/Markets/Korea/Japan/China&HK/Taiwan/Crypto/AI sections. Generated via `generate_brief.py` subprocess (Haiku model). Cached 1hr, force-refresh via `?force=1`.
- **Tech Brief** — Separate semiconductor/tech news digest, 24hr window. Generated via `generate_tech_brief.py`. Sources include Digitimes, TechNews, Commercial Times, UDN, Anue, etc.
- **Top Headlines / Portfolio firehoses** — Google News RSS with approved source filtering (Bloomberg, WSJ, FT, Reuters, Politico, Axios, NYT + Asian tech sources). Chinese headlines auto-translated via Claude.
- **Events Calendar** — ForexFactory red events (current week via API, future weeks via Playwright scraping cached in `_ff_calendar_cache.json`) + company events from `config/dashboard.json`.
- **Company pages** — Filings, research notes, classified documents per ticker.
- **Sidebar** — Companies grouped by country (collapsible), sorted by name. Display format: `Advantest (6857 JT)`.

### Key Config Files
- `config/companies.json` — 140 companies with market, IR URL, ir_extra_urls, regulatory IDs (sec_cik, edinet_code, dart_code, twse_code), fiscal_year_end
- `config/dashboard.json` — News topics, portfolio positions, brief sections, company events, macro events, timezone (Asia/Hong_Kong), feed_sources

## Coverage (as of 2026-03-19)

### Companies: 140
| Market | Count | Downloader | Coverage |
|--------|-------|------------|----------|
| US | 35 | IR scraper + SEC EDGAR (10-K, 10-Q, 20-F, 6-K) | Full — 20-25 filings per company |
| Japan | 38 | IR scraper + EDINET (有価証券報告書, 四半期報告書, 半期報告書) + TDNet (決算短信) | Full — 20-30 filings per company |
| Taiwan | 50 | IR scraper + MOPS (quarterly financial statements + monthly revenue) | Full — 28 quarterly + ~46 monthly revenue PDFs per company |
| Korea | 17 | IR scraper + DART (사업보고서, 분기보고서, 반기보고서) | Full — 24-26 filings per company |

### Total Files Downloaded
- IR downloads: 3,638 PDFs
- SEC EDGAR: 882 PDFs
- EDINET: 643 PDFs
- DART: 419 PDFs
- MOPS quarterly: 1,004 PDFs
- MOPS monthly revenue: 1,922 PDFs
- Organized filings: 3,571 PDFs
- Sellside research notes: 4
- Macro research notes: 36 (15 authors)
- Thematic research notes: 6 (1 theme: WFE)

### Macro Research Authors (36 notes)
Alyosha (Market Vibes), Alexander Campbell (Campbell Ramble), Brent Donnelly (Spectra Markets), Chase Taylor, Danny Dayan, David Cervantes (Pinebrook Capital), Geo Chen (Fidenza Macro), Kevin Muir (MacroTourist), Le Shrub, Michael Howell, Paper Alfa (PA Global Macro), PauloMacro, Richard Excell (Stay Vigilant), Unknown, News Article

### Thematic Research
- WFE (Wafer Fab Equipment): 6 notes from Nomura, UBS, Barclays (2), BofA, Goldman Sachs. Linked tickers: KLAC, ASML, 2330 TT, 005930 KS.

### Sellside Research
- 4004 JT (Resonac): Nomura — 1 note
- KLAC: Barclays, BofA — 2 notes
- 3037 TT (Unimicron): Nomura — 1 note

## Directory Structure

```
research-pipeline/
├── main.py                          # CLI entry point (20 commands)
├── app.py                           # Flask web GUI
├── generate_brief.py                # Morning/Evening Brief generator (subprocess)
├── generate_tech_brief.py           # Tech Brief generator (subprocess)
├── refresh_ff_calendar.py           # ForexFactory calendar scraper
├── config/
│   ├── companies.json               # 140 companies
│   └── dashboard.json               # Dashboard config (news topics, events, brief structure)
├── scripts/
│   ├── ir_scraper.py                # IR website scraper (Playwright, accordion expansion, deep mode)
│   ├── sec_edgar.py                 # SEC EDGAR downloader (10-K, 10-Q, 20-F, 6-K)
│   ├── edinet.py                    # Japan EDINET downloader (date-scanning with scan_state cache)
│   ├── tdnet.py                     # Japan TDNet scraper (tanshin/決算短信)
│   ├── dart.py                      # Korea DART downloader (API-based)
│   ├── mops.py                      # Taiwan MOPS downloader (Vue SPA form + monthly revenue)
│   ├── classifier.py                # Document classifier (Claude Sonnet)
│   ├── extractor.py                 # Financial data extractor (Claude Sonnet)
│   ├── model_builder.py             # Excel model builder (openpyxl)
│   ├── research.py                  # Single-name research storage + analysis
│   ├── macro.py                     # Macro research with view history tracking
│   ├── thematic.py                  # Thematic/sector research with cross-referencing
│   ├── filing_gaps.py               # Filing gap analysis (expected vs actual periods)
│   └── analysis_report.py           # Shared analysis report generation (2-pass Claude)
├── templates/                       # Flask Jinja2 templates
│   ├── base.html                    # Sidebar + topbar layout
│   ├── dashboard.html               # Dashboard with briefs, firehoses, calendar
│   └── company.html                 # Company detail page
├── static/
│   └── style.css                    # Claude light theme (Söhne font, cream bg, warm brown accents)
├── data/
│   ├── {TICKER}/                    # Per-company data
│   │   ├── ir/                      # IR website downloads
│   │   ├── sec/ or edinet/ or dart/ # Regulatory raw downloads
│   │   ├── mops/                    # MOPS quarterly + monthly_revenue/
│   │   ├── filings/                 # Organized filings (Annual/, Quarterly/, Tanshin/, SemiAnnual/)
│   │   ├── classifications.json     # Classified document metadata
│   │   ├── research/                # Sellside research (notes/, summary.json, summary.md)
│   │   └── financials/              # Extracted financial data
│   ├── Macro/                       # Macro research
│   │   ├── authors/{name}/notes/    # Notes per author with view history
│   │   ├── themes/                  # Theme files (fed_policy.json, etc.)
│   │   ├── macro_summary.json/md    # Cross-author summary
│   │   ├── _brief_cache.json        # Cached morning/evening brief
│   │   └── _tech_brief_cache.json   # Cached tech brief
│   └── Thematic/                    # Thematic research
│       └── {THEME}/                 # notes/, theme_summary.json/md, linked_tickers.json
└── models/                          # Excel models
```

## Key Implementation Details

### IR Scraper (`ir_scraper.py`)
- Playwright with `ignore_https_errors=True` and accordion expansion
- `_expand_accordions()` clicks all accordion/dropdown/toggle elements before scanning
- `ir_extra_urls` in companies.json for sub-pages the crawler can't find
- `--deep` flag follows links 2 levels deep
- SSL error fallback: retries with Playwright on SSL failures
- Handles API-style download URLs (e.g. `/Activity?Action=Get_IRFinancialReport_FileName&Id=124`) via `_is_downloadable()` checking for download/getfile/attachment patterns
- Source list for approved news: `DEFAULT_SOURCES` + `TECH_SOURCES` + `ALL_APPROVED_SOURCES`

### MOPS (`mops.py`)
- Vue.js SPA form interaction: fill `#companyId` → click "自訂" label → fill `#year` (ROC) + `#season` → click `#searchBtn`
- Renders financial statement HTML as PDF via `page.pdf()`
- Monthly revenue: direct URL pattern `emops.twse.com.tw/server-java/t05st10_e?step=show&co_id={code}&year={year}&month={month}`
- Saves to `data/{ticker}/mops/` and `data/{ticker}/mops/monthly_revenue/`

### EDINET (`edinet.py`)
- Date-by-date scanning (API only searches by date, not company)
- `scan_state.json` caches last scanned date for resumable scanning
- Skip weekends, 0.5s delay between requests
- Downloads PDF directly when `pdfFlag=1` (type=2 returns PDF, not ZIP)
- Falls back to ZIP extraction → HTML→PDF conversion

### DART (`dart.py`)
- API-based filing search (type "A" = regular reports)
- Gets `dcmNo` from filing viewer page via Playwright
- Downloads PDF from `dart.fss.or.kr/pdf/download/pdf.do` using `expect_download`

### Filing Gap Analysis (`filing_gaps.py`)
- Maps classified IR documents to fiscal periods
- Compares against expected periods (since_year → today)
- Japan quarterly reform: quarterly reports abolished from FY2024 (March FY-end) / FY2025 (Dec FY-end), but companies still publish voluntary quarterly results
- Semi-annual H1 counts as Q2 coverage

### Research Storage
- **Two-pass analysis**: First API call extracts structured data + Key Points + Trader's Brief. Second call generates View Evolution & Cross-Author Comparison.
- **View history tracking** (macro): Each topic tracks current view + chronological history (capped at 50 entries)
- **Currency awareness** (single-name): JPY/TWD/KRW companies get proper currency symbols in summaries

### Brief Generation
- Runs as subprocess to avoid Flask connection pool conflicts
- Chunked approach: headlines split into batches of 8, processed in parallel via ThreadPoolExecutor
- Section merging: fuzzy-matches chunk outputs to valid section names, filters invalid sections
- Bullet normalization: all bullets converted to `- ` format
- Chinese headline translation via Claude batch call
- Google News RSS: searches en-US + zh-TW + zh-CN feeds
- Chinese keyword mapping for tech terms (半導體, 晶圓代工, ABF載板, etc.)

## Known Issues / Incomplete

1. **2 macro notes failed to store** — Richard Excell "But, what if?" and CMR 03.15.2026. API connection drops. PDFs are copied to author folders, just need JSON extraction retry.
2. **MOPS not implemented for OTC/TPEx stocks** — current MOPS only works for TWSE main board (TYPEK=sii). OTC stocks need TYPEK=otc.
3. **Flask zombie processes** — Windows doesn't always kill Python processes cleanly. Server auto-finds free ports (5080-5099) to work around.
4. **Anthropic API connection drops** — Intermittent `RemoteProtocolError` on larger payloads. Mitigated with `max_retries=3`, `timeout=180.0`, fresh client per retry. Brief generation uses Haiku (more reliable than Sonnet for this machine).
5. **ForexFactory** only provides current week via API. Future weeks scraped via Playwright (cached 12hrs in `_ff_calendar_cache.json`). Weekend data may be empty.
6. **Some TW company IR sites** still return 0 files despite correct URLs — sites like Acer, Largan, Innolux block automated scraping entirely. MOPS covers their filings.

## Environment
- Python 3.12, Windows 11
- Key packages: flask, anthropic, playwright, pdfplumber, openpyxl, click, requests, beautifulsoup4, markdown
- API keys in `.env`: ANTHROPIC_API_KEY, EDINET_API_KEY, DART_API_KEY
- Timezone: Asia/Hong_Kong (HKT)
