# Session Progress — 2026-06-07/08

One-page summary of the work in this session. Atomic detail lives in `git log`.

## What was built

### 1. Telegram bot (`bot.py`)
Single-user allowlist gateway to the pipeline. Default flow: drop a PDF, bot
auto-classifies, stores under the right entity, and runs cross-cutting analysis
vs every other ticker/theme it touches. Manual overrides:
`/research TICKER`, `/macro AUTHOR`, `/thematic THEME`.

### 2. Auto-classification (`scripts/triage.py`, `scripts/bot_pipeline.py`)
Claude-based document triage. Routes each doc to one of:
- `single_name` → `data/{TICKER}/research/notes/`
- `macro` + `category=Macro` → `data/Macro/authors/{author}/notes/`
- `macro` + `category=Semis` → `data/Semis/authors/{author}/notes/` (NEW)
- `thematic` → `data/Thematic/{theme}/notes/`
- `news_article` → catch-all under Macro

Strict theme constraint — never auto-invents themes; surfaces proposals for
review instead. Auto-adds new tickers and authors to inventory on the fly.

Prompt caching on the static inventory (instructions + 226 tickers + themes +
authors) via `cache_control: ephemeral`. Confirmed working — `write=7127` on
first call, `read=7127` on subsequent calls.

### 3. Semiconductors author category (`scripts/semis.py`)
New sibling to `data/Macro/`. Reuses `scripts/macro.py` logic by rebinding
`macro.MACRO_DIR` at runtime. Authors split: 12 true-macro voices
(Warren Pies, Luke Gromen, 22V analysts, etc.) and 18 semis-equity outlets
(SemiAnalysis, Chipstrat, Tessara, FundaAI, Damnang, PhotonCap,
Vikram Sekar, Ben Thompson Stratechery, etc.).

### 4. Bulk-ingest CLI (`scripts/bulk_ingest.py`, `python main.py bulk-ingest`)
One-shot backfill loader. Triages every PDF in the inbox folder, stores under
primary type only (no cross-cuts). Resumable via SHA256 hash in
`data/_bulk_ingest_state.json`. `--dry-run` for cost estimation,
`--limit N` for partial runs, `--with-cross-cut` to also fire the cross-cut.

### 5. Bulk-cross-cut CLI (`scripts/bulk_cross_cut.py`, `python main.py bulk-cross-cut`)
Second-pass over the corpus. For every stored doc, runs analyse_research /
analyse_thematic against each *other* ticker/theme it touches. Resumable, cost
estimate + yes/no prompt before spending. Skips pairs already done.

### 6. Folder sweeper (`scripts/sweeper.py`, `python main.py sweep`)
Long-running watchdog process. Watches an inbox folder for new PDFs, runs the
full ingest pipeline, pushes results to Telegram per file. Settle window of
~6s for partial writes (Syncthing chunked writes, browser downloads). Skips
files already in bulk-ingest or sweeper state. Designed to run on the Mac mini
as a LaunchAgent in production.

### 7. Pipeline hardening (`scripts/research.py`, `scripts/macro.py`)
- Model bumped: Sonnet 4 → **Opus 4.7**
- Streaming fallback added to `research.py` `_call_api` (was missing); both
  files now check `stop_reason` after streaming and retry on truncation
  rather than returning partial chunks
- JSON-decode retry wraps the extraction call in `store_research` / `store_macro`
- `max_tokens` 8192 → 16384 on extraction + view-evolution
- Shared `Anthropic()` client threaded from bulk-ingest → triage so prompt
  cache hits persist across files

### 8. Approved additions (companies.json, folder structure)
- **76 new tickers** added (150 → 226): 51 clean US (LPTH, MRVL, AAOI, SITM,
  CRWV, etc.), 12 Asia/EU verified via web search (AWX SP AEM Holdings,
  6728 JT Ulvac, 6855 JT Japan Electronic Materials, 402340 KS SK Square,
  ATS AV AT&S, etc.), plus SPCX (SpaceX, upcoming IPO).
- **12 new Macro authors** (17 → 29).
- **18 new Semis authors** (0 → 18, new category).
- **7 new themes** (6 → 13): AI Infrastructure, Agentic AI, EDA, AI Inference,
  Fuel Cells, Data Center Power, Quantum Computing.

## Current state

- **Bulk-ingest is running in background** on the desktop, processing 137 PDFs
  from `C:\Users\Owner\Downloads\research-inbox\`. Log:
  `bulk_ingest_real.log`. State: `data/_bulk_ingest_state.json` (5 processed
  already: 2 successes + 3 failures from a pre-hardening run; failures will
  retry on this restart).
- **Bot is running** (background python, PID changes per session). Always-on
  until this Claude Code session ends. Production target = Mac mini LaunchAgent.
- **Estimated bulk-ingest cost** (Opus 4.7 with caching): ~$130–200.

## Architecture: where things will live in production

| | Today (development) | Production |
|---|---|---|
| `bot.py` | Desktop | Mac mini LaunchAgent |
| `python main.py sweep` | Not yet running | Mac mini LaunchAgent |
| Canonical `data/` | Desktop | Mac mini (syncthing one-way mirror from desktop's `~/Downloads/research-inbox/`) |
| Claude Code sessions | Desktop, this session | Desktop, via git pull from private repo |
| Code | Local clone | Git on both machines |

## Open / next session

1. **Wait for bulk-ingest to finish** — Telegram summary on completion. Inspect failures.
2. **Bulk cross-cut pass** — `python main.py bulk-cross-cut --dry-run` to see cost estimate, then real run (~$75–100, gives every stored doc cross-cuts against every entity it touches).
3. **Mac mini port** — install deps, clone repo, rsync `data/`, set up Syncthing one-way mirror for `research-inbox/`, write LaunchAgent plists for `bot.py` and `sweep`.
4. **Rotate the bot token** — it was pasted in chat early in the session.
5. **Heartbeat / agenda.md scheduler** — the twice-daily folder sweep + 2-hourly headlines were scoped but not built.
6. **Email sweep** — IMAP for Substack inbox.
7. **Update CLAUDE.md** — pre-session version is now significantly out of date.

## Quick reference

```powershell
# Start bot (desktop, for testing)
cd ~/research-pipeline; python bot.py

# Start sweeper (production target = Mac mini, but works on desktop)
cd ~/research-pipeline; python main.py sweep

# Bulk-ingest the inbox (one-shot backfill)
cd ~/research-pipeline; python main.py bulk-ingest --dry-run   # plan + cost
cd ~/research-pipeline; python main.py bulk-ingest             # real run

# Second-pass cross-cuts
cd ~/research-pipeline; python main.py bulk-cross-cut --dry-run
cd ~/research-pipeline; python main.py bulk-cross-cut

# Tail a running ingest
Get-Content C:\Users\Owner\research-pipeline\bulk_ingest_real.log -Wait -Tail 30
```

## State files (gitignored, in `data/`)

- `_bulk_ingest_state.json` — what bulk-ingest has processed/failed
- `_bulk_cross_cut_state.json` — what cross-cut pairs are done
- `_sweeper_state.json` — what the realtime sweeper has handled

## Git history this session

```
c12265e Add 76 new tickers (51 US + 12 Asia/EU + SPCX) to companies.json
74ea274 Pipeline hardening: Opus 4.7 + prompt caching + streaming/JSON retry
70e9c5b Add real-time folder sweeper for the production inbox
cfcd809 Add bulk-ingest, bulk-cross-cut, and apply-approved CLI commands
9e17aae Add Telegram bot, auto-classification pipeline, and Semis category
c1c56f6 Initial commit — equity research automation platform (pre-session)
```
