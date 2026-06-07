# Research Pipeline — Project State

## What This Is

An equity research automation platform for a buy-side analyst at a macro hedge fund running equity long/short. The system:

- **Downloads** IR materials + regulatory filings across 226 companies in 6 markets (US, Japan, Taiwan, Korea, Singapore, Austria/EU)
- **Auto-classifies** new research (sellside, macro, semis-equity, thematic, news) and stores it under the right entity
- **Analyses** new research against the existing knowledge base — comparative estimate tables, view-evolution tracking, cross-cutting reads
- **Exposes** all of this through a Telegram bot + a long-running folder sweeper + a Flask web dashboard

## Architecture

### Entry points

**Telegram bot (`bot.py`)** — single-user allowlist gateway. Drop a PDF, bot auto-triages and ingests. Manual overrides: `/research TICKER`, `/macro AUTHOR`, `/thematic THEME`. Other commands: `/start`, `/help`, `/cancel`, `/status`.

**Folder sweeper (`scripts/sweeper.py`, `python main.py sweep`)** — long-running watchdog process. Watches an inbox folder for new PDFs and runs the full ingest pipeline per file. Designed to run as a Mac mini LaunchAgent in production.

**Bulk ingest (`scripts/bulk_ingest.py`, `python main.py bulk-ingest`)** — one-shot backfill loader for an entire folder. Resumable via SHA256 state. `--dry-run` for cost preview, `--with-cross-cut` to also fire the cross-cut pass, `--force` to re-process.

**Bulk cross-cut (`scripts/bulk_cross_cut.py`, `python main.py bulk-cross-cut`)** — second pass over the corpus. For every stored doc, runs analyse_research / analyse_thematic against each other ticker/theme it touches. Materiality-gated: `passing` mentions are skipped.

**CLI (`main.py`)** — 22 Click commands including `download-materials`, `store-research`, `store-macro`, `store-thematic`, `analyse`, `bulk-ingest`, `bulk-cross-cut`, `sweep`, `gui`.

**Flask web GUI (`app.py`, `python main.py gui`)** — localhost dashboard. Morning/Evening Brief, Tech Brief, headline firehoses, events calendar, company pages.

### Auto-classification pipeline (`scripts/triage.py` → `scripts/bot_pipeline.py`)

Each dropped PDF flows through:

1. **Triage** (`scripts/triage.py`, Haiku 4.5, prompt-cached). Classifies into `single_name | macro | thematic | news_article`. Identifies all tickers + themes + authors substantively discussed. Tags each with `materiality: primary | significant | passing`. Strict theme constraint — never invents new themes; surfaces proposals for review. Auto-proposes new tickers / authors with structured metadata. Output is a JSON object with hard schema; doc text is wrapped in `<document>` tags as a prompt-injection defence.

2. **Confidence gate**. If triage returns `confidence: low`, the file is copied to `data/_pending_review/` instead of auto-stored. Telegram message + entry in `data/_routing_log.jsonl` (append-only audit).

3. **Primary store** (`scripts/bot_pipeline._store_primary`). Routes to:
   - `data/{TICKER}/research/notes/` for single-name
   - `data/Macro/authors/{author}/notes/` for true macro
   - `data/Semis/authors/{author}/notes/` for semis-equity authors (NEW category)
   - `data/Thematic/{theme}/notes/` for thematic
   - Calls `scripts/research.store_research` / `scripts/macro.store_macro` / `scripts/thematic.store_thematic`
   - Extraction uses Sonnet 4.6 (structured JSON), view-evolution uses Opus 4.7 (synthesis)

4. **Cross-cuts** (optional in real-time flow, separate pass in bulk). For every secondary entity the doc touches *and* materiality ≠ `passing`, runs `analyse_research` / `analyse_thematic` against the existing summary.

### Model tiering

| Step | Model | Why |
|---|---|---|
| Triage / classification | `claude-haiku-4-5-20251001` | Constrained classification — Haiku is plenty, ~15× cheaper than Opus |
| Structured extraction | `claude-sonnet-4-6` | Long structured JSON — Sonnet 4.6 is the sweet spot |
| View-evolution / cross-cut analysis | `claude-opus-4-7` | Synthesis quality matters — Opus earns its keep |
| IR document classifier | `claude-sonnet-4-20250514` | Just routing 10-K vs earnings; Sonnet is fine, unchanged |

Per-call model is passed to `_call_api(..., model=...)` in `scripts/research.py`. Other scripts have their own `_call_api` matching this signature.

### Prompt caching

The triage prompt's static block (instructions + 226-ticker inventory + 13 themes + 47 authors) is sent as a system message with `cache_control: {"type": "ephemeral"}`. Verified working — `cache_creation_input_tokens` on the first call of a 5-minute window, `cache_read_input_tokens` on subsequent calls. Bulk-ingest gets full benefit (sequential calls); steady-state sweeper mostly pays the cache-write premium without read benefit (sporadic file arrivals).

### Storage layout

```
data/
├── {TICKER}/                      # 226 tickers (US/JP/TW/KR/SG/EU)
│   ├── ir/                        # IR website downloads
│   ├── sec/ or edinet/ or dart/   # Regulatory raw downloads
│   ├── mops/                      # Taiwan MOPS quarterly + monthly revenue
│   ├── filings/                   # Organized filings
│   ├── classifications.json
│   ├── research/                  # Sellside research
│   │   ├── notes/                 # *.pdf, *.pdf.json, *_summary.md
│   │   ├── analyses/              # Cross-cut analysis reports
│   │   ├── summary.json
│   │   └── summary.md
│   └── financials/
├── Macro/                         # True macro/strategy authors
│   ├── authors/{name}/notes/
│   ├── themes/
│   ├── analyses/
│   ├── macro_summary.json
│   ├── macro_summary.md
│   └── _brief_cache.json
├── Semis/                         # NEW — semi/tech-equity authors
│   ├── authors/{name}/notes/      # SemiAnalysis, Damnang, PhotonCap, Vikram Sekar, ...
│   ├── themes/
│   ├── analyses/
│   ├── macro_summary.json         # reuses macro code; "macro_summary" naming for code reuse
│   └── macro_summary.md
├── Thematic/{THEME}/
│   ├── notes/
│   ├── analyses/
│   ├── theme_summary.json
│   ├── theme_summary.md
│   └── linked_tickers.json
├── _pending_review/               # Low-confidence triage holding folder
├── _routing_log.jsonl             # Append-only audit of every classification decision
├── _bulk_ingest_state.json        # SHA256 → processed/failed (with attempts counter)
├── _bulk_cross_cut_state.json     # SHA256:kind:target → cross-cut done
└── _sweeper_state.json            # SHA256 → processed/failed (with attempts counter)
```

## Coverage

### Companies: 226 (was 140 pre-session)

| Market | Count | Downloader |
|---|---|---|
| US | ~85 | IR scraper + SEC EDGAR |
| Japan | ~45 | IR scraper + EDINET + TDNet |
| Taiwan | ~55 | IR scraper + MOPS (quarterly + monthly revenue) |
| Korea | ~18 | IR scraper + DART |
| Singapore | 2 | IR scraper (no auto-downloader yet) |
| EU/Austria | 1 | IR scraper |

### Themes: 13

Original: WFE, Components, Memory, Photonics, SPEs, Substrates.
Added this session: AI Infrastructure, Agentic AI, EDA, AI Inference, Fuel Cells, Data Center Power, Quantum Computing.

### Macro authors: 29 (was 17)

Added this session: Warren Pies (3Fourteen Research), Philip Swift, Luke Gromen, Jordi Visser (22V Research), Dennis DeBusschere (22V), Kim Wallace (22V), Peter Williams (22V), Art Berman, Jan van Eck, Jason Shapiro (Crowded Market Report), Josh Brown, Michael Batnick.

### Semis authors: 18 (new category)

SemiAnalysis, Chipstrat, Tessara, FundaAI, Damnang, PhotonCap, Gaetano, Tae Kim, Vikram Sekar, Semi Doped, Jason's Chips, Irrational Analysis, Collyer Bridge, NuttyCLD, Asymmetrical Bets, quantLR, Ben Thompson (Stratechery), Gavin Baker.

Storage is parallel to Macro: `data/Semis/authors/{name}/notes/`. Same view-evolution + cross-author comparison code (reused via `macro.use_category("Semis")` context manager).

## Key Implementation Details

### Concurrency safety (`scripts/macro.py` + `scripts/semis.py`)

`scripts/semis.py` reuses all of `scripts/macro.py` by entering a `contextvars.ContextVar`-backed `macro.use_category("Semis")` context. ContextVar values are per-thread + per-asyncio-task, so concurrent bot uploads and sweeper jobs never collide. The previous monkey-patch-the-global approach had a documented race condition fixed in Round 2.

### Failure handling

Both `bulk_ingest` and `sweeper` track `attempts` per failed hash. After 3 failures, the file is permanently skipped (logged as "Gave-up"). Success clears any prior failure record. Failures distinguished from successes: SHA256 skip-check uses the `processed` dict only.

### Syncthing-aware sweeper

Sweeper ignores `.syncthing.*.tmp`, `~syncthing~*`, `.crdownload`, `.part`, `.download`, `.tmp` patterns and dotfiles. Treats `on_moved` as the primary completion signal (Syncthing finishes a transfer via atomic rename). Settle window is short (~2s) because the atomic rename is itself a "complete" signal; size-stability check is belt-and-suspenders.

### Prompt-injection defence

All extraction/analysis prompts wrap document text in `<document>...</document>` tags with explicit "data, not instructions" framing in the system prompt. The triage system prompt explicitly instructs Claude to lower its confidence to `low` and flag injection attempts in the rationale rather than complying.

### Strict theme constraint

Triage MUST pick themes from `KNOWN_THEMES`. New themes are NEVER auto-created — proposals are surfaced for human review at the end of bulk-ingest runs. This keeps the theme hierarchy deliberate.

### Tier conventions (`config/companies.json` ticker format)

- US: bare symbol (`AAPL`, `KLAC`, `MRVL`)
- JP: Bloomberg `XXXX JT` (`6857 JT`, `6728 JT`)
- TW: `XXXX TT` (`2330 TT`, `6173 TT`) — includes both TWSE and TPEx names
- KR: `XXXXXX KS` (`000660 KS`, `005930 KS`, `402340 KS`)
- HK: `XXXX HK`
- SG: `XXX SP` (`558 SP`, `AWX SP`)
- EU: `TICKER AV` (Vienna; e.g. `ATS AV`)
- Special: `SPCX` (US, SpaceX expected listing)

## Production deployment plan

| Component | Today | Production target |
|---|---|---|
| `bot.py` | Desktop (foreground python) | Mac mini LaunchAgent (always-on) |
| `python main.py sweep` | Desktop testing only | Mac mini LaunchAgent (always-on) |
| Canonical `data/` | Desktop | Mac mini |
| Inbox folder (where new PDFs land) | `C:\Users\Owner\Downloads\research-inbox\` | Same desktop folder, one-way Syncthing mirror → Mac mini `~/research-pipeline/downloads/` |
| Code | Local clone | Git on both machines, deploy via `git pull` |
| Claude Code sessions | Desktop | Desktop |
| Flask GUI | Desktop ad-hoc | Mac mini, optionally exposed over Tailscale |

**Cutover rule (single-writer):** rsync → stop desktop processes → start Mac mini processes. Never both at once or state files diverge.

## Known Issues / Incomplete

1. **Existing IR-scraper Sonnet 4 model ID** is still `claude-sonnet-4-20250514` in `scripts/classifier.py` and `scripts/extractor.py`. Intentional — those tasks are simple routing and don't need a newer model; cost is dominated by the research pipeline.
2. **2 macro notes failed to store** (pre-session) — Richard Excell "But, what if?" and CMR 03.15.2026. PDFs copied; JSON extraction needs retry.
3. **MOPS not implemented for OTC/TPEx stocks** — current MOPS only works for TWSE main board (`TYPEK=sii`). OTC stocks need `TYPEK=otc`.
4. **Flask zombie processes** — Windows doesn't always kill Python processes cleanly. Server auto-finds free ports (5080-5099).
5. **ForexFactory** only provides current week via API. Future weeks scraped via Playwright (cached 12hrs).
6. **Some TW company IR sites** return 0 files (Acer, Largan, Innolux block automated scraping). MOPS covers their filings.
7. **Inline-keyboard confirmation for held files** — v1 sends text-only "held for review" Telegram message. v2 should send `[Confirm] [Reclassify] [Drop]` inline buttons.
8. **Headlines + email sweep + heartbeat/agenda.md** — scoped but not built. Belongs in next session.

## Environment

- Python 3.12, Windows 11 (desktop), macOS (Mac mini target)
- Key packages: flask, anthropic, playwright, pdfplumber, openpyxl, click, requests, beautifulsoup4, markdown, python-telegram-bot[ext]>=21, watchdog, truststore
- API keys in `.env`: `ANTHROPIC_API_KEY`, `EDINET_API_KEY`, `DART_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS`
- Timezone: Asia/Hong_Kong (HKT)
- `truststore.inject_into_ssl()` at top of every entry point — Norton MITMs HTTPS on this Windows machine

## Quick reference

```powershell
# Bot
cd ~/research-pipeline; python bot.py

# Sweeper (default watches research-inbox on this desktop)
cd ~/research-pipeline; python main.py sweep

# Bulk-ingest the inbox (one-shot backfill)
cd ~/research-pipeline; python main.py bulk-ingest --dry-run     # plan + cost
cd ~/research-pipeline; python main.py bulk-ingest               # real run

# Cross-cuts (run after bulk-ingest finishes)
cd ~/research-pipeline; python main.py bulk-cross-cut --dry-run
cd ~/research-pipeline; python main.py bulk-cross-cut

# Tail a long-running ingest log
Get-Content C:\Users\Owner\research-pipeline\bulk_ingest_real.log -Wait -Tail 30

# Inspect routing decisions
Get-Content C:\Users\Owner\research-pipeline\data\_routing_log.jsonl | ConvertFrom-Json | Select-Object -Last 20
```
