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
| View-evolution / cross-cut analysis | `claude-opus-4-8` | Synthesis quality matters — Opus earns its keep |
| IR document classifier | `claude-haiku-4-5-20251001` | Just routing 10-K vs earnings; Haiku is plenty, ~3× cheaper than Sonnet 4 |

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

## Features added 2026-06-23/24

### Chinese company name recognition (`config/companies.json` + `scripts/kb.py`)

34 companies now have a `name_zh` field (30 TW, 4 KR). `extract_entities()` checks `name_zh in raw_haystack` (not lowercased — Chinese has no case) as a third match condition after ticker symbol and English name. Fixes "structured KB is empty" false negatives on all-Chinese headlines (e.g. "聯發科" → 2454 TT).

### Text snippet ingestion (`bot.py`)

Start any Telegram message with `ingest this [note] [by AUTHOR]`, then paste the body. Bot saves as `{author}_{timestamp}.txt` in `data/_telegram_uploads/`, queues an `ingest_file` job. Triage sees the filename (author hint) + text body and classifies normally. Identical pipeline to PDF drops.

### Inbox scan digest (`scripts/folder_scan.py`, `scripts/jobs.py`, `bot.py`)

After `folder_scan` with `notify=True` queues new files:
- Saves `data/_latest_inbox_scan.json` with `{scan_id, total, items: []}`.
- Each `ingest_file` job (when `scan_id` is in payload) appends `{title, author, stored_path, json_path, triage}` to the digest; last job to complete sends the Telegram digest.
- Digest format mirrors email sweep: rank, bold title, author, `/inbox_N` command.
- `/inbox_N` → `analyse_inbox_file` job → reloads stored JSON + re-runs `research_readthrough` (full Opus synthesis).

## Known Issues / Incomplete

1. **`scripts/classifier.py` runs Haiku 4.5** (`claude-haiku-4-5-20251001`) for IR-doc routing. **`scripts/extractor.py` now runs `claude-sonnet-4-6`** (2026-07-02 — the old `claude-sonnet-4-20250514` passed its retirement date).
2. **MOPS not implemented for OTC/TPEx stocks** — current MOPS only works for TWSE main board (`TYPEK=sii`). OTC stocks need `TYPEK=otc`.
3. **Flask zombie processes** — Windows doesn't always kill Python processes cleanly. Server auto-finds free ports (5080-5099).
4. **ForexFactory** only provides current week via API. Future weeks scraped via Playwright (cached 12hrs).
5. **Some TW company IR sites** return 0 files (Acer, Largan, Innolux block automated scraping). MOPS covers their filings.
6. **Inline-keyboard confirmation for held files** — v1 sends text-only "held for review" Telegram message. v2 should send `[Confirm] [Reclassify] [Drop]` inline buttons.
7. **Standalone inbox digest failure mode** — non-combined folder-scan digests still require every `ingest_file` job to succeed before sending (combined-mode digests are covered by the failsafe + terminal-failure notices as of 2026-07-02).
8. **CJK FTS tokenization** — `_fts_query` extracts only `[A-Za-z0-9_]+` tokens and the FTS table uses unicode61, so all-Chinese queries get zero keyword hits (the full-corpus vector scan now covers them semantically, but `name_zh` keyword search remains a gap).
9. **Dual ingest pipelines diverge** — the fast job pipeline (`parallel_ingest`) and the classic pipeline (sweeper/bulk) keep separate SHA-state namespaces, and the fast path skips `_routing_log.jsonl`, held-for-review notices, and `summary.json` rebuilds. Don't run the sweeper alongside job-based folder scans on the same inbox.

## Vector index migration 2026-07-03 (branches worktree-vec-migration + worktree-json-drop)

Embeddings moved from JSON text to float32 BLOBs (`chunks.embedding`), the
legacy `embedding_json` column was dropped, and the DB was VACUUMed:
**42 GB (dual-format peak) -> 15 GB**. A pre-drop backup lives at
`data/_kb/kb_backup_predrop.sqlite` (42 GB, integrity-checked; delete it once
the new stack has proven itself for a few days).

- **Sidecar vector index** (`data/_kb/vec_index/`): an L2-normalized
  memory-mapped float32 matrix of all ~952k chunk embeddings, in versioned
  dirs behind an atomically-replaced `current.json` pointer (Windows-lock
  safe). `kb.search` runs a **full-corpus numpy scan** for unfiltered
  queries — every embedded chunk is semantically reachable (the old
  candidate-pool recall ceiling is gone). Chunks newer than the sidecar are
  scored live; source-filtered queries use the bounded pool path.
- **Nightly upkeep**: the 03:00 `kb_reindex` job runs `embed_migrate()`
  (no-ops now the column is dropped) and `build_vector_index()` so the
  sidecar always includes the previous day's ingests.
- **Commands**: `kb-vec-build` (manual sidecar rebuild), `kb-embed-backfill`
  (embed missing chunks; migrates legacy JSON first if a legacy DB is ever
  attached), `kb-embed-migrate` (legacy conversion; no-op here),
  `kb-drop-json --vacuum` (already executed 2026-07-03).
- Fresh DBs are created blob-only; all reads are BLOB-first with a guarded
  legacy-JSON fallback, so the code also works against pre-migration DBs.

## Audit fixes 2026-07-02 (branch worktree-audit-fixes)

Full audit + fix session (see AUDIT_PLAN.md and the branch's commit messages).
Highlights that change day-to-day behavior:

- **Telegram delivery is verified** (`scripts/notify.py`): status checked,
  429/5xx retried honoring retry_after, failures logged, send functions
  return a success bool. Terminal job failures (attempts exhausted or worker
  death on the final attempt) now send a ⚠️ Telegram notice.
- **Combined 03:00 digest** is race-free (cross-process lockfile, atomic
  state, claim-before-send); failsafe is 90 min from last activity
  (`COMBINED_DIGEST_STALE_MINUTES`); late parts are sent as "(late)"
  follow-ups instead of being dropped.
- **KB embeddings**: `index_text` persists `embedding_error` on the document
  row; `python main.py kb-embed-backfill` repairs unembedded chunks (the
  2026-06-14..16 reindex wave left 461k chunks — 48% — unembedded and
  invisible); nightly reindex stats report `unembedded_chunks`.
- **Hybrid search is genuinely hybrid**: reciprocal-rank fusion replaced the
  broken max(bm25,0)+cosine sum (keyword hits all scored 1.0; semantic hits
  could never outrank them).
- **Job system**: `jobs.claimed_by` (worker PID) makes the stale reaper
  liveness-aware — a live worker's job is never requeued mid-run (the old
  45-min cutoff violated the single-writer lane); dead-owner jobs recover
  immediately; lane-filtered reaping; worker lanes + heartbeat hold
  singleton locks in `data/_kb/*.lock`; heartbeat survives bad ticks;
  success bookkeeping can no longer convert a finished job into a retry.
- **Injection framing everywhere**: scraped headlines/articles, analyst
  readthroughs, kb.ask sources, classifier/extractor prompts, native-PDF
  extraction payloads, and `<research_memory>` blocks (with tag/attribute
  escaping) all carry data-not-instructions framing;
  ANALYST_SYSTEM_PROMPT has an untrusted-content section + grounding
  discipline ("what would change my mind", staleness flags).
- **Half-stored notes self-repair**: a `.json` still containing
  PENDING_SECOND_PASS (crash between extraction save and the Opus second
  pass) is treated as incomplete — re-dropping the file re-processes it.
- Encoding: the 13 remaining `open()` calls without `encoding=` are fixed
  (PYTHONUTF8=1 is still required for anything missed and for stdio).
- Study cost gate uses real Opus pricing ($5/$25; was $15/$75 — studies were
  stopping at a third of their budget). GUI translation runs Haiku.
- Flask GUI runs with debug=False. Edited Telegram messages are handled
  (edited questions re-ask; they no longer crash the handler).

## Environment

- Python 3.12, Windows 11 (desktop), macOS (Mac mini target)
- Key packages: flask, anthropic, playwright, pdfplumber, openpyxl, click, requests, beautifulsoup4, markdown, python-telegram-bot[ext]>=21, watchdog, truststore
- API keys in `.env`: `ANTHROPIC_API_KEY`, `EDINET_API_KEY`, `DART_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS`
- Timezone: Asia/Hong_Kong (HKT)
- `truststore.inject_into_ssl()` at top of every entry point — Norton MITMs HTTPS on this Windows machine

## Quick reference

```powershell
# After reboot: restart all four services (no auto-start on Windows)
# PYTHONUTF8=1 is REQUIRED: default Windows encoding is cp1252, and several
# config reads (companies.json has Chinese name_zh fields) decode as UTF-8.
# Without it, single-name ingests crash with UnicodeDecodeError (charmap).
$env:PYTHONUTF8 = "1"; $env:PYTHONIOENCODING = "utf-8"
$py = "C:\Users\Owner\AppData\Local\Programs\Python\Python312\python.exe"
$root = "C:\Users\Owner\research-pipeline"
Start-Process -FilePath $py -ArgumentList "bot.py" -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput "$root\bot.out.log" -RedirectStandardError "$root\bot.err.log"
Start-Process -FilePath $py -ArgumentList "main.py worker --exclude-kinds analyst_question" -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput "$root\worker.out.log" -RedirectStandardError "$root\worker.err.log"
Start-Process -FilePath $py -ArgumentList "main.py worker --kinds analyst_question" -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput "$root\worker_interactive.out.log" -RedirectStandardError "$root\worker_interactive.err.log"
Start-Process -FilePath $py -ArgumentList "main.py heartbeat" -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput "$root\heartbeat.out.log" -RedirectStandardError "$root\heartbeat.err.log"

# Bot (foreground, for debugging)
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

# Find duplicate note PDFs (byte-identical); --apply quarantines extras
cd ~/research-pipeline; python main.py dedup-notes            # dry-run + manifest
cd ~/research-pipeline; python main.py dedup-notes --apply    # refuses while worker has a running job

# Repair KB chunks stored without embeddings (resumable; --dry-run to count)
cd ~/research-pipeline; python main.py kb-embed-backfill --dry-run
cd ~/research-pipeline; python main.py kb-embed-backfill
```

## KB index (full corpus)

The KB covers the entire data tree — research notes AND all IR/filings/MOPS
PDFs (~36.6k documents, ~952k chunks, all embedded, OpenAI
text-embedding-3-small stored as float32 BLOBs; DB ~15 GB post-migration).
The original backfill ran 2026-06-11; a second embedding backfill repaired
the 461k chunks left unembedded by the June 14-16 reindex wave (2026-07-02),
and the BLOB/sidecar migration completed 2026-07-03 (see the Vector index
migration section). Reindex cost control:
- `index_file` skips unchanged files via a mtime+size stamp in
  `documents.metadata_json` — no text extraction for unchanged files.
- Failed/empty extractions (corrupt or image-only PDFs, ~130 files) are
  stamped in `data/_kb/extract_failure_stamps.json` and skipped until the
  file changes; they have no documents row so they'd otherwise re-extract
  every night.
- `kb-reindex --parallel N` fans extraction out to N processes (backfills);
  `--limit` counts new/changed files only.
The nightly 03:00 `kb_reindex` job (source=all) is therefore a stat-scan
plus only genuinely new files — minutes, pennies.

## Adaptive thinking (analyst + study)

The analyst synthesis and study-dossier Anthropic calls run Opus with
**adaptive thinking on** (`thinking={"type":"adaptive"}`) plus an `effort`
level — the model reasons before answering. Wired via `call_api(..., thinking=,
effort=)` (off by default, so triage/extraction ingestion calls are
unaffected) and `analyst._resolve_thinking(prefix)`. Env control:
`ANALYST_THINKING`/`ANALYST_EFFORT` and `STUDY_THINKING`/`STUDY_EFFORT`
(study falls back to ANALYST_* then defaults). `*_THINKING`: adaptive (default)
| off. `*_EFFORT`: low|medium|high(default)|xhigh|max. Thinking tokens share
the output budget, so analyst/study `max_tokens` defaults were raised to 8000.

## Agentic analyst (Telegram bot)

`scripts/analyst.answer_question` runs a streamed Anthropic tool-use loop
(`_call_claude_agentic`): `run_pipeline_job` (whitelisted kinds:
headline/email/folder sweeps + reindexes — "run the tech brief" queues a
headline_sweep), `pipeline_status`, and `search_kb` follow-up retrieval.
Falls back to plain single-shot synthesis on any failure or when
ANALYST_PROVIDER=openai; disable with ANALYST_TOOLS=0. Missed headline
slots also self-heal: heartbeat catch_up runs same-day missed slots on
startup.

## Worker lanes

Two worker processes run in parallel lanes so analyst questions never queue
behind multi-minute document ingests:

```powershell
python main.py worker --exclude-kinds analyst_question   # heavy lane (serial: ingests, sweeps, reindexes)
python main.py worker --kinds analyst_question           # interactive lane (read-mostly)
```

The heavy lane must stay a single process — ingest jobs rebuild per-entity
summary files and would race each other. The claim query is one atomic
UPDATE, so lanes partition the queue safely. The poll loop survives
transient `database is locked` from concurrent CLI reindexes/backfills.

## Duplicate handling

Store paths skip re-storing a byte-identical file that already exists in the
destination notes dir under any date prefix (`scripts/dedup_notes.existing_identical_copy`).
The `dedup-notes` command cleans historical duplicates: keeper = canonical-location
copy, richest extraction transplanted onto it, losers moved to
`data/_dedup_quarantine/<ts>/` with flattened names plus a dump of every DB row
removed. Groups whose copies resolve to two real subjects (same PDF deliberately
filed under two tickers/authors/themes) are reported in the manifest and never
touched — their research rows carry per-subject granular data.
