---
timezone: Asia/Hong_Kong
folder: C:\Users\Owner\Downloads\research-inbox
folder_sweep_times: [08:30, 20:30]
email_sweep_times: [01:00, 13:00]
headline_sweep_times: [02:00, 08:00, 14:00, 20:00]
headline_interval_hours: 6
headline_window_hours: 24
headline_max_items: 20
reindex_times: [03:00]
study_times: [03:30]
notify: true
folder_analyse: false
email_analyse_attachments: false
email_extract_research: true
---

# Research Pipeline Agenda

This file controls the always-on heartbeat scheduler.

- Folder scans enqueue PDFs into the worker.
- Email sweeps ingest Substack/research messages, parse attached `.eml` research emails, run structured extraction, and queue PDF attachments.
- Folder and email sweeps index PDFs silently by default; they do not send full analyst read-throughs unless the analyse flags above are set to true.
- Headline sweeps run at 02:00, 08:00, 14:00, and 20:00 HKT, storing raw matches and sending the ranked top-20 Tech Brief over a 24-hour recency window (configurable via headline_window_hours / headline_max_items). Undated scraped items use first-seen time for the recency check, so stale headlines re-appearing on source index pages are filtered out. Fresh-only: headlines already delivered in a prior brief are not resent (toggle with HEADLINE_FRESH_ONLY=0), so the overlapping 24h windows don't repeat the same items.
- The worker is the only long-running process that performs heavy ingestion.
- A nightly reindex (03:00 HKT) sweeps all stored notes into the full-text KB and structured research memory, so research stored via the Telegram bot / watchdog sweeper becomes searchable without manual `kb-reindex` runs.
- A nightly study run (03:30 HKT) refreshes company/theme dossiers for targets touched by documents ingested in the last ~30 hours (cost-capped at ~$15/night; a quiet day studies nothing).
