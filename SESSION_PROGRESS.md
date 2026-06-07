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

## Round 2 (post design-review): 8 fixes applied

1. **Model tiering** — Haiku 4.5 for triage (classification), Sonnet 4.6 for extraction (structured JSON), Opus 4.7 reserved for synthesis (view-evolution, cross-cut). Roughly ~60% steady-state cost cut. `_call_api` now accepts a `model=` param; per-call constants `EXTRACTION_MODEL` / `SYNTHESIS_MODEL` express the tier choice.
2. **MACRO_DIR race condition** — replaced module-level monkey-patching with a `contextvars.ContextVar` and `macro.use_category(category)` context manager. Thread-safe and asyncio-safe; concurrent bot uploads + sweeper jobs no longer risk silently routing a Semis doc into `data/Macro/` or vice versa.
3. **Cross-cut materiality gate** — every ticker/theme in triage output now carries `materiality: primary | significant | passing`. `_derive_secondaries` drops `passing` mentions. A SemiAnalysis EDA primer that mentions 22 tickers now produces ~10 cross-cuts instead of 22.
4. **Prompt-injection defence** — all extraction/analysis prompts wrap doc text in `<document>...</document>` tags with explicit "data, not instructions" framing in the system prompt. Triage's system prompt explicitly tells the model to flag override attempts with `confidence: low` rather than complying.
5. **Failure-state retry cap (3 attempts)** — both `bulk_ingest` and `sweeper` now track `attempts` per failed hash. After 3 fails, file is skipped on subsequent runs. Successful processing clears the prior failure record.
6. **Syncthing-aware sweeper** — ignores `.syncthing.*.tmp`, `~syncthing~*`, `.crdownload`, `.part`, `.download`, `.tmp` patterns and dotfiles. Settle window shortened from ~6s to ~2s because Syncthing's atomic rename-into-place is itself a "complete" signal; we still verify size stability as belt-and-suspenders.
7. **Low-confidence holding folder** — when triage returns `confidence: low`, file goes to `data/_pending_review/` instead of auto-storing. Telegram message explains the best-guess classification and lists override commands.
8. **Routing log** — every classification decision (held or stored) is appended to `data/_routing_log.jsonl`. One JSON object per line for easy `jq` / pandas review.

## Policy items (documentation — applies at Mac mini cutover)

- **Inbox lifecycle:** files stay in the inbox forever; SHA256-based state dedupes. Schedule a monthly archive job on the desktop side (the source) that moves files >90 days old to a separate archive folder. The Mac mini must NEVER delete inbox files — Syncthing would resurrect them or flag conflicts.
- **Single-writer rule for state files:** during the Mac mini cutover, run in this order: (1) rsync `data/` desktop → mini, (2) stop bot + sweeper on desktop, (3) start bot + sweeper on mini. Never both at once or `_*_state.json` files will diverge.
- **Backups:** `data/` will embody ~$200 of API spend + irreplaceable notes once bulk-ingest runs. Set up Time Machine to an external drive on the Mac mini AND a nightly `restic` or `rclone` snapshot from mini → desktop during the port. Don't wait for the first scare.
- **Cache TTL realism:** ephemeral cache has ~5min TTL. Bulk-ingest hits it (sequential calls); the production sweeper mostly won't (sporadic file arrivals). Steady-state, every dropped PDF pays the 1.25× cache-write premium without a corresponding read. Don't extrapolate backfill economics to production.
- **Bot persistence on desktop:** even for testing, run via `Start-Process` (Windows) or `nohup` (Mac) so it survives the launching shell closing.
- **Ticker inventory scaling:** currently 226 tickers at ~7K tokens fits comfortably in the cached system block. At ~50–100K tokens (probably ~500+ tickers), switch to: embedding the doc, retrieving top-K relevant tickers, passing only those to triage. Watch this when count crosses ~400.

## Open / next session

1. **Restart bulk-ingest** — with all the Round 2 fixes in. Expect ~$30–60 (Haiku triage + Sonnet extraction + Opus synthesis). Resumable from current state (3 already processed).
2. **Bulk cross-cut pass** — `python main.py bulk-cross-cut --dry-run` first. Materiality gate makes this cheaper than originally estimated.
3. **Mac mini port** — see deployment steps above.
4. **Heartbeat / agenda.md scheduler** — twice-daily folder sweep + 2-hourly headlines, all keyed off a hand-editable `agenda.md`.
5. **Email sweep** — IMAP for Substack inbox.
6. **Inline-keyboard confirmation for held files** — v2 of low-confidence routing: send `[Confirm] [Reclassify] [Drop]` buttons via Telegram instead of a text-only message.

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
