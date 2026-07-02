# Task: Audit, debug, and optimise the research pipeline

## Context
This repo is a buyside equity research automation pipeline with three moving parts:
1. **Telegram bot analyst** — conversational interface that should perform at the level of a top-tier hedge fund analyst by combining (a) the local knowledge base (SQLite at `data/_kb/kb.sqlite` with OpenAI text-embedding-3-small vectors, plus markdown/JSON summaries in the `data/` tree), (b) web search, and (c) Claude Opus 4.8 reasoning
2. **Two worker lanes** — background ingestion/processing workers
3. **Heartbeat scheduler** — detached Python process on Windows today (no auto-start after reboot; APScheduler also runs inside the bot); launchd/LaunchAgent on a Mac mini is the production target

Read CLAUDE.md and any README/docs first to load prior session state and conventions before touching anything.

## Phase 1 — Map and diagnose (no code changes)
- Build a mental model of the full data flow: message in → retrieval → tool calls → model calls → response; and separately: scheduler → workers → DB/vault writes
- Identify every failure mode you can find: unhandled exceptions, silent failures, race conditions between the two worker lanes, DB connection leaks, vector-search query correctness, scheduler job overlap/duplicate-run risk (heartbeat + bot APScheduler), Telegram API rate limits and long-poll/webhook edge cases, stale locks, retry logic (or absence of it)
- Check logging/observability: can I tell from logs alone why any given run failed?
- Check secrets handling: no tokens/keys in code, git history, or logs. Flag anything that needs rotation.
- Check prompt injection surface: content from ingested PDFs, web pages, and sellside docs must be treated as untrusted data, never as instructions. Verify workers and the bot enforce this separation.
- Check least-privilege: each lane should only have the DB/filesystem/API permissions it needs.
- Check model-tiering economics: Haiku/Sonnet for triage/classification, Opus only where reasoning quality matters. Flag any call using a more expensive model than needed, missing prompt caching, or missing batching.

**Deliverable:** a prioritised findings report (P0 = data corruption/security/silent failure, P1 = reliability, P2 = cost/perf, P3 = code quality). STOP and show me this report before making changes.

## Phase 2 — Fix (after my approval of the plan)
- Fix in priority order, one logical change per commit, with clear commit messages
- Add tests for every bug fixed (regression tests), and add basic integration tests for the bot's retrieval → answer path if none exist
- Do not refactor working code for style alone; do not change behaviour of thesis-state logic without flagging it (human-in-the-loop approval is a hard design constraint)
- Preserve existing conventions (file layout, naming, CLAUDE.md state pattern)

## Phase 3 — Optimise the analyst quality
The bot's output should read like a top-tier HF analyst memo, not a chatbot. Evaluate and improve:
- **Retrieval:** is vector-search recall actually good? Check chunking strategy, embedding model, hybrid search (keyword + vector), metadata filtering by ticker/date, and whether the bot cites which KB documents it used
- **Grounding:** every number in an answer must be attributable to a KB source or a live web fetch — no fabricated figures. Add explicit instructions and verification for this in the analyst system prompt
- **Synthesis:** the analyst prompt should demand thesis-first structure, second-order effects, what-would-change-my-mind, and flag when KB data is stale vs. fresh web data
- **Freshness:** when should the bot prefer web search over KB (prices, recent filings, news) and does it do so correctly?
- Propose concrete system-prompt improvements as a diff, don't just describe them

## Phase 4 — Report
Summarise: what was broken, what you fixed, what you optimised, estimated cost savings from model-tiering changes, remaining known risks, and a short list of recommended next steps I should prioritise.

## Rules
- Ask before any destructive operation (DB migrations, deleting files, rewriting git history)
- Never log or echo secrets
- If you find a live credential anywhere, stop and tell me immediately
