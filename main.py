import truststore; truststore.inject_into_ssl()  # Use Windows cert store (handles Norton SSL inspection)

import sys
# Force UTF-8 on stdout/stderr so Unicode in summaries doesn't blow up the Windows cp1252 console.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import json
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import click

CONFIG_PATH = Path(__file__).parent / "config" / "companies.json"


def load_companies():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_companies(data):
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


@click.group()
def cli():
    """Research Pipeline — equity research automation CLI."""
    pass


@cli.command("download-materials")
@click.argument("ticker")
@click.option("--limit", default=200, show_default=True,
              help="Max number of files to download per run.")
@click.option("--since", default=2020, show_default=True,
              type=click.IntRange(1990, 2100),
              help="Only download documents from this year onward (based on URL/filename date).")
@click.option("--deep", is_flag=True,
              help="Follow links two levels deep instead of one (slower but more thorough).")
def download_materials(ticker, limit, since, deep):
    """Download IR materials, filings, and disclosures for TICKER."""
    from scripts.ir_scraper import scrape_ir

    companies = load_companies()
    if ticker not in companies:
        click.echo(f"Error: ticker '{ticker}' not found in companies.json")
        click.echo("Run: python main.py add-company")
        raise SystemExit(1)

    company = companies[ticker]
    market = company.get("market", "US")

    # Step 1: IR scraper (all markets)
    scrape_ir(ticker, limit=limit, since_year=since, deep=deep)

    if market == "US":
        # --- US workflow: IR scraper → full SEC EDGAR → classify → organize ---
        if company.get("sec_cik"):
            from scripts.sec_edgar import download_sec_filings
            download_sec_filings(ticker, since_year=since)

        from scripts.classifier import classify_documents
        click.echo("")
        classify_documents(ticker)

        if company.get("sec_cik"):
            from scripts.sec_edgar import organize_filings
            click.echo("")
            organize_filings(ticker)
    else:
        # --- Asian workflow: IR → classify → organize → gap analysis → fill gaps ---
        from scripts.classifier import classify_documents
        from scripts.filing_gaps import organize_ir_filings, analyze_gaps, print_gap_report

        click.echo("")
        classify_documents(ticker)

        # Organize classified IR files into filings/
        click.echo("")
        n = organize_ir_filings(ticker)
        if n:
            click.echo(f"Organized {n} IR file(s) into filings/")

        # Analyze gaps
        gaps = analyze_gaps(ticker, since)
        has_gaps = print_gap_report(ticker, gaps)

        if not has_gaps:
            return

        # Fill gaps with regulatory downloaders
        if market == "JP":
            if gaps["missing_annual"] or gaps["missing_quarterly"]:
                click.echo(f"\n  Checking EDINET for gaps...")
                from scripts.edinet import download_edinet_filings
                download_edinet_filings(ticker, since_year=since)

            # Re-check after EDINET
            gaps = analyze_gaps(ticker, since)
            if gaps["missing_annual"] or gaps["missing_quarterly"]:
                click.echo(f"\n  Checking TDNet for remaining gaps...")
                from scripts.tdnet import download_tdnet_filings
                download_tdnet_filings(ticker, since_year=since)

        elif market == "TW":
            click.echo(f"\n  Checking MOPS for gaps...")
            from scripts.mops import download_mops_filings
            download_mops_filings(ticker, since_year=since)

        elif market == "KR":
            click.echo(f"\n  Checking DART for gaps...")
            from scripts.dart import download_dart_filings
            download_dart_filings(ticker, since_year=since)

        # Final gap report
        gaps = analyze_gaps(ticker, since)
        print_gap_report(ticker, gaps)


@cli.command()
@click.argument("ticker")
@click.option("--reclassify", is_flag=True, help="Force re-classification of all files.")
def classify(ticker, reclassify):
    """Classify downloaded IR documents for TICKER using Claude."""
    from scripts.classifier import classify_documents

    companies = load_companies()
    if ticker not in companies:
        click.echo(f"Error: ticker '{ticker}' not found in companies.json")
        raise SystemExit(1)
    classify_documents(ticker, reclassify=reclassify)


@cli.command()
@click.argument("ticker")
@click.option("--re-extract", is_flag=True, help="Force re-extraction of all files.")
def extract(ticker, re_extract):
    """Extract structured financial data from classified filings for TICKER."""
    from scripts.extractor import extract_financials

    companies = load_companies()
    if ticker not in companies:
        click.echo(f"Error: ticker '{ticker}' not found in companies.json")
        raise SystemExit(1)
    extract_financials(ticker, re_extract=re_extract)


@cli.command()
@click.argument("ticker")
def model(ticker):
    """Build / refresh the Excel model for TICKER."""
    from scripts.model_builder import build_model

    companies = load_companies()
    if ticker not in companies:
        click.echo(f"Error: ticker '{ticker}' not found in companies.json")
        raise SystemExit(1)
    build_model(ticker)


@cli.command("store-research")
@click.argument("ticker")
@click.argument("files", nargs=-1, required=True, type=click.Path(exists=True))
def store_research(ticker, files):
    """Store one or more research documents for TICKER and rebuild the living summary.

    Usage: python main.py store-research TICKER file1.pdf [file2.pdf ...]
    """
    from scripts.research import store_research as _store

    companies = load_companies()
    if ticker not in companies:
        click.echo(f"Error: ticker '{ticker}' not found in companies.json")
        raise SystemExit(1)
    for i, file_path in enumerate(files):
        if i > 0:
            click.echo(f"\n{'='*60}\n")
        _store(ticker, file_path)


@cli.command()
@click.argument("ticker")
@click.option("--file", "file_path", type=click.Path(exists=True),
              help="Path to a new research file to analyse.")
@click.option("--headline", type=str,
              help="Headline text to analyse against the research base.")
def analyse(ticker, file_path, headline):
    """Analyse new research or a headline against the stored research base for TICKER."""
    from scripts.research import analyse_research

    companies = load_companies()
    if ticker not in companies:
        click.echo(f"Error: ticker '{ticker}' not found in companies.json")
        raise SystemExit(1)
    if not file_path and not headline:
        click.echo("Error: must provide either --file or --headline")
        raise SystemExit(1)
    analyse_research(ticker, file_path=file_path, headline=headline)


@cli.command("show-summary")
@click.argument("ticker")
def show_summary(ticker):
    """Print the research summary for TICKER."""
    from scripts.research import show_summary as _show

    companies = load_companies()
    if ticker not in companies:
        click.echo(f"Error: ticker '{ticker}' not found in companies.json")
        raise SystemExit(1)
    _show(ticker)


@cli.command("create-theme")
def create_theme_cmd():
    """Interactively create a new thematic research topic."""
    from scripts.thematic import create_theme
    create_theme()


@cli.command("store-thematic")
@click.argument("theme")
@click.argument("files", nargs=-1, required=True, type=click.Path(exists=True))
def store_thematic(theme, files):
    """Store one or more thematic research documents for THEME.

    Usage: python main.py store-thematic "WFE" file1.pdf [file2.pdf ...]
    """
    from scripts.thematic import store_thematic as _store
    _store(theme, files)


@cli.command("analyse-thematic")
@click.argument("theme")
@click.option("--file", "file_path", required=True, type=click.Path(exists=True),
              help="Path to a thematic research file to analyse.")
def analyse_thematic(theme, file_path):
    """Analyse thematic research with cross-referencing against single-name research."""
    from scripts.thematic import analyse_thematic as _analyse
    _analyse(theme, file_path)


@cli.command("show-theme-summary")
@click.argument("theme")
def show_theme_summary(theme):
    """Print the thematic research summary for THEME."""
    from scripts.thematic import show_theme_summary as _show
    _show(theme)


@cli.command("list-themes")
def list_themes_cmd():
    """List all thematic research topics and their linked tickers."""
    from scripts.thematic import list_themes
    list_themes()


@cli.command("edit-theme")
@click.argument("theme")
def edit_theme_cmd(theme):
    """Edit linked tickers and key metrics for THEME."""
    from scripts.thematic import edit_theme
    edit_theme(theme)


@cli.command("store-macro")
@click.option("--file", "file_path", required=True, type=click.Path(exists=True),
              help="Path to the macro research file.")
@click.option("--author", required=True, help="Author name, e.g. 'Michael Hartnett (BofA)'")
def store_macro_cmd(file_path, author):
    """Store a macro research document and track view evolution."""
    from scripts.macro import store_macro
    store_macro(file_path, author)


@cli.command("analyse-macro")
@click.option("--file", "file_path", required=True, type=click.Path(exists=True),
              help="Path to a macro research file to analyse.")
def analyse_macro_cmd(file_path):
    """Analyse new macro research against stored views across all authors."""
    from scripts.macro import analyse_macro
    analyse_macro(file_path)


@cli.command("show-macro-summary")
def show_macro_summary_cmd():
    """Print the cross-author macro research summary."""
    from scripts.macro import show_macro_summary
    show_macro_summary()


@cli.command("show-author-summary")
@click.option("--author", required=True, help="Author name.")
def show_author_summary_cmd(author):
    """Print an author's current macro views and recent changes."""
    from scripts.macro import show_author_summary
    show_author_summary(author)


@cli.command("show-author-history")
@click.option("--author", required=True, help="Author name.")
@click.option("--topic", default=None, help="Specific topic (e.g. monetary_policy). Omit for all topics.")
def show_author_history_cmd(author, topic):
    """Print an author's view evolution timeline."""
    from scripts.macro import show_author_history
    show_author_history(author, topic)


@cli.command("list-macro-authors")
def list_macro_authors_cmd():
    """List all macro research authors and their note counts."""
    from scripts.macro import list_macro_authors
    list_macro_authors()


@cli.command("refresh-events")
@click.option("--days", default=60, show_default=True,
              help="How many days ahead to scan for events.")
@click.option("--force", is_flag=True, help="Force refresh even if cache is fresh.")
def refresh_events_cmd(days, force):
    """Refresh the automated events calendar (earnings, revenue dates, etc.)."""
    from scripts.events_calendar import refresh_events
    refresh_events(days_ahead=days, force=force)


@cli.command("add-company")
def add_company():
    """Interactively add a new company to companies.json."""
    ticker = click.prompt("Ticker (e.g. AAPL, 4004 JT)")
    name = click.prompt("Company name")
    market = click.prompt(
        "Market",
        type=click.Choice(["US", "JP", "TW", "KR", "other"], case_sensitive=False),
    )
    ir_url = click.prompt("IR website URL")

    fy_end = click.prompt(
        "Fiscal year-end month",
        type=click.Choice([
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ], case_sensitive=False),
        default="December",
    )

    entry = {"name": name, "market": market, "ir_url": ir_url, "fiscal_year_end": fy_end}

    # Extra IR URLs
    extra_urls = []
    click.echo("Extra IR URLs (e.g. sub-pages with filings/transcripts). Type 'done' to finish:")
    while True:
        url = click.prompt("  URL", default="done", show_default=False)
        if url.lower() == "done":
            break
        extra_urls.append(url)
    if extra_urls:
        entry["ir_extra_urls"] = extra_urls

    reg_prompts = {
        "US": ("sec_cik", "SEC CIK (e.g. 0000320193)"),
        "JP": ("edinet_code", "EDINET code (e.g. E00847)"),
        "TW": ("twse_code", "TWSE code (e.g. 2330)"),
        "KR": ("dart_code", "DART code (e.g. 00126380)"),
    }

    if market in reg_prompts:
        key, prompt_text = reg_prompts[market]
        value = click.prompt(prompt_text, default="", show_default=False)
        if value:
            entry[key] = value

    companies = load_companies()

    if ticker in companies:
        if not click.confirm(f"'{ticker}' already exists. Overwrite?"):
            click.echo("Aborted.")
            return

    companies[ticker] = entry
    save_companies(companies)
    click.echo(f"Added {ticker} ({name}) to companies.json.")


@cli.command("bulk-ingest")
@click.option("--folder", default=r"C:\Users\Owner\Downloads\research-inbox",
              show_default=True,
              help="Folder of PDFs to ingest (recursive).")
@click.option("--dry-run", is_flag=True,
              help="Triage only — no storage. Use to preview classifications and estimate cost.")
@click.option("--limit", default=0, type=int,
              help="Process at most this many new files (0 = unlimited).")
@click.option("--with-cross-cut", is_flag=True,
              help="Also run cross-cutting analysis per file (2-5x cost/time).")
@click.option("--force", is_flag=True,
              help="Re-process files already recorded in state.")
@click.option("--no-notify", is_flag=True,
              help="Skip the Telegram summary at the end.")
def bulk_ingest_cmd(folder, dry_run, limit, with_cross_cut, force, no_notify):
    """Bulk-ingest a folder of PDFs into the knowledge base.

    Default: triage + store under primary type. Skips files already processed
    (tracked by content hash in data/_bulk_ingest_state.json). Safe to re-run.
    """
    from scripts.bulk_ingest import bulk_ingest
    bulk_ingest(
        folder=folder,
        dry_run=dry_run,
        limit=limit,
        with_cross_cut=with_cross_cut,
        force=force,
        notify=not no_notify,
    )


@cli.command("bulk-ingest-fast")
@click.option("--folder", default=r"C:\Users\Owner\Downloads\research-inbox",
              show_default=True,
              help="Folder of PDFs to stage/ingest (recursive by default).")
@click.option("--parallel", default=4, show_default=True, type=click.IntRange(1, 12),
              help="Parallel read-only extraction workers.")
@click.option("--limit", default=0, type=int,
              help="Process at most this many uncommitted files (0 = unlimited).")
@click.option("--force", is_flag=True,
              help="Re-stage/re-commit files even if already processed.")
@click.option("--stage-only", is_flag=True,
              help="Only run the parallel LLM stage; do not write into data/ or KB.")
@click.option("--commit-only", is_flag=True,
              help="Commit existing staged files without making LLM calls.")
@click.option("--no-recursive", is_flag=True,
              help="Only scan the top-level folder.")
@click.option("--no-embed", is_flag=True,
              help="Build FTS/raw KB index but skip OpenAI embeddings.")
def bulk_ingest_fast_cmd(folder, parallel, limit, force, stage_only, commit_only, no_recursive, no_embed):
    """Fast provider-pluggable ingest: parallel extraction, single-writer commit."""
    from scripts.parallel_ingest import bulk_ingest_fast

    if stage_only and commit_only:
        click.echo("Error: --stage-only and --commit-only are mutually exclusive.")
        raise SystemExit(1)

    try:
        stats = bulk_ingest_fast(
            folder=folder,
            parallel=parallel,
            limit=limit,
            force=force,
            stage_only=stage_only,
            commit_only=commit_only,
            recursive=not no_recursive,
            embed=not no_embed,
        )
    except Exception as e:
        raise click.ClickException(str(e)) from e
    click.echo(json.dumps(stats, indent=2, ensure_ascii=False))


@cli.command("bulk-ingest-fast-status")
@click.option("--folder", default=r"C:\Users\Owner\Downloads\research-inbox",
              show_default=True,
              help="Folder to compare against staged/committed state.")
def bulk_ingest_fast_status_cmd(folder):
    """Show resumable fast-ingest staging and commit status."""
    from scripts.parallel_ingest import fast_ingest_status

    click.echo(json.dumps(fast_ingest_status(folder), indent=2, ensure_ascii=False))


@cli.command("bulk-cross-cut")
@click.option("--folder", default=r"C:\Users\Owner\Downloads\research-inbox",
              show_default=True,
              help="Fallback folder for resolving source PDFs by filename if the path recorded by bulk-ingest is gone.")
@click.option("--dry-run", is_flag=True,
              help="Show plan + estimated cost only; no API calls.")
@click.option("--limit", default=0, type=int,
              help="Process at most this many pairs (0 = unlimited).")
@click.option("--yes", is_flag=True,
              help="Skip the cost confirmation prompt.")
@click.option("--no-notify", is_flag=True,
              help="Skip the Telegram summary at end.")
def bulk_cross_cut_cmd(folder, dry_run, limit, yes, no_notify):
    """Second-pass: cross-cutting analysis on every doc bulk-ingest stored.

    For each stored doc, runs analyse_research / analyse_thematic against every
    OTHER ticker/theme it touches. Resumable. Skips pairs already done. Warns
    on cost before running (~$0.30/pair, ~$75-100 for a 117-doc corpus).
    """
    from scripts.bulk_cross_cut import bulk_cross_cut
    bulk_cross_cut(
        folder=folder,
        dry_run=dry_run,
        limit=limit,
        yes=yes,
        notify=not no_notify,
    )


@cli.command("sweep")
@click.option("--folder", default=r"C:\Users\Owner\Downloads\research-inbox",
              show_default=True,
              help="Folder to watch for new PDFs.")
@click.option("--with-cross-cut", is_flag=True,
              help="Also run cross-cutting analysis per file (2-5x cost/time).")
@click.option("--scan-existing", is_flag=True,
              help="At startup, also process any PDFs already in the folder.")
def sweep_cmd(folder, with_cross_cut, scan_existing):
    """Watch a folder for new PDFs and ingest them (long-running).

    Skips files already processed by bulk-ingest (matched by content hash).
    Pushes triage + storage + cross-cut results to Telegram per file.
    """
    from scripts.sweeper import sweep
    sweep(folder=folder, with_cross_cut=with_cross_cut, scan_existing=scan_existing)


@cli.command("worker")
@click.option("--once", is_flag=True, help="Process at most one queued job, then exit.")
@click.option("--sleep", "sleep_seconds", default=5, show_default=True,
              help="Seconds to sleep when no job is queued.")
def worker_cmd(once, sleep_seconds):
    """Run the single-writer job worker."""
    from scripts.jobs import worker
    worker(run_once=once, sleep_seconds=sleep_seconds)


@cli.command("heartbeat")
@click.option("--agenda", default="agenda.md", show_default=True,
              help="Path to agenda.md with schedule front matter.")
@click.option("--once", is_flag=True, help="Evaluate the agenda once, enqueue due jobs, then exit.")
def heartbeat_cmd(agenda, once):
    """Run the agenda scheduler that enqueues folder/email/headline jobs."""
    from scripts.heartbeat import heartbeat
    heartbeat(agenda_path=agenda, run_once=once)


@cli.command("dedup-notes")
@click.option("--apply", "apply_changes", is_flag=True,
              help="Quarantine duplicate files and prune their KB/research rows. "
                   "Default is a dry-run that only writes a manifest.")
def dedup_notes_cmd(apply_changes):
    """Find byte-identical duplicate note PDFs; quarantine extras with --apply.

    Only deduplicates copies that resolve to the same canonical subject; the
    richest extraction in each group is preserved on the kept copy. Groups
    spanning different subjects are reported in the manifest, not touched.
    """
    from scripts.dedup_notes import run_dedup
    outcome = run_dedup(apply=apply_changes)
    plan = outcome["plan"]
    click.echo(json.dumps({k: v for k, v in plan.items() if k != "groups"},
                          indent=2, ensure_ascii=False))
    if outcome["applied"]:
        applied = {k: v for k, v in outcome["applied"].items() if k != "db_dump"}
        click.echo(json.dumps(applied, indent=2, ensure_ascii=False))
    click.echo(f"Manifest: {outcome['manifest_path']}")


@cli.command("kb-reindex")
@click.option("--source", default="all", show_default=True,
              type=click.Choice(["all", "research", "ir", "filings", "claude", "email", "headlines", "company", "skills", "notes"],
                                case_sensitive=False),
              help="Corpus slice to index.")
@click.option("--force", is_flag=True, help="Re-index documents even if their hash is unchanged.")
@click.option("--limit", default=0, type=int, help="Index at most N files (0 = unlimited).")
@click.option("--no-embed", is_flag=True,
              help="Skip embedding creation and build only SQLite metadata/FTS.")
def kb_reindex_cmd(source, force, limit, no_embed):
    """Build or refresh the local KB index over data/."""
    from scripts.kb import reindex_source
    stats = reindex_source(source=source, force=force, limit=limit, embed=not no_embed)
    click.echo(json.dumps(stats, indent=2, ensure_ascii=False))


@cli.command("reingest-clipped")
@click.option("--folder", default=None, help="Inbox folder to scan (default: research-inbox).")
@click.option("--threshold", default=30000, show_default=True, type=int,
              help="Docs whose extracted text exceeds this many chars were clipped by the old ceiling.")
@click.option("--apply", is_flag=True,
              help="Clear the clipped docs' ingest-state hashes so the next bulk-ingest / folder sweep re-processes them. Without this flag, just list them.")
def reingest_clipped_cmd(folder, threshold, apply):
    """Find docs clipped by the old 30K-char extraction ceiling and queue re-ingestion."""
    from scripts.reingest_clipped import reingest_clipped

    stats = reingest_clipped(folder=folder, threshold=threshold, apply=apply)
    click.echo(json.dumps(stats, indent=2, ensure_ascii=False))
    if stats["clipped"] and not apply:
        click.echo("\nDry run only. Re-run with --apply, then run: python main.py bulk-ingest")


@cli.command("reingest-stale-schema")
@click.option("--cutoff", default=None,
              help="UTC ISO instant; stage records older than this are re-queued (default: the schema-evolution commit time).")
@click.option("--apply", is_flag=True,
              help="Clear the stale docs' ingest-state hashes and staging records. Without this flag, just list them.")
def reingest_stale_schema_cmd(cutoff, apply):
    """Re-queue docs extracted before the enriched schema (segments/valuation/primer fields)."""
    from scripts.reingest_clipped import SCHEMA_CUTOFF_UTC, reingest_stale_schema

    stats = reingest_stale_schema(cutoff=cutoff or SCHEMA_CUTOFF_UTC, apply=apply)
    click.echo(json.dumps(stats, indent=2, ensure_ascii=False))
    if stats["stale"] and not apply:
        click.echo("\nDry run only. Re-run with --apply, then run: python main.py bulk-ingest-fast")


@cli.command("research-map-reindex")
@click.option("--force", is_flag=True, help="Rebuild structured research-memory tables from scratch.")
@click.option("--limit", default=0, type=int, help="Index at most N extracted JSON notes (0 = unlimited).")
def research_map_reindex_cmd(force, limit):
    """Build structured author views, estimates, ratings, and thesis-change tables."""
    from scripts.research_memory import rebuild

    stats = rebuild(force=force, limit=limit)
    click.echo(json.dumps(stats, indent=2, ensure_ascii=False))


@cli.command("research-map-status")
def research_map_status_cmd():
    """Show structured research-memory coverage and extraction backlog."""
    from scripts.research_memory import extraction_backlog, status

    stats = status()
    stats["backlog_sample"] = extraction_backlog(limit=12)
    click.echo(json.dumps(stats, indent=2, ensure_ascii=False))


@cli.command("research-map-show")
@click.argument("subject")
@click.option("--limit", default=8, show_default=True, help="Rows per section.")
def research_map_show_cmd(subject, limit):
    """Show the structured memory snapshot for a ticker, theme, or author."""
    from scripts.research_memory import subject_snapshot

    click.echo(subject_snapshot(subject, limit=limit))


@cli.command("import-claude-export")
@click.argument("zip_or_dir", type=click.Path(exists=True))
@click.option("--dry-run", is_flag=True, help="Count conversations without writing or indexing.")
@click.option("--force", is_flag=True, help="Overwrite exported Markdown and re-index.")
def import_claude_export_cmd(zip_or_dir, dry_run, force):
    """Import an official Claude export ZIP/directory into the KB."""
    from scripts.claude_export import import_claude_export
    stats = import_claude_export(zip_or_dir, dry_run=dry_run, force=force)
    click.echo(json.dumps(stats, indent=2, ensure_ascii=False))


@cli.command("import-skills")
@click.argument("source_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--dry-run", is_flag=True, help="Count skill files without copying or indexing.")
@click.option("--force", is_flag=True, help="Overwrite copied skill files and re-index.")
def import_skills_cmd(source_dir, dry_run, force):
    """Import Claude/Codex skill files into data/_skills and the KB."""
    from scripts.skills_import import import_skills
    stats = import_skills(source_dir, dry_run=dry_run, force=force)
    click.echo(json.dumps(stats, indent=2, ensure_ascii=False))


@cli.command("email-sweep")
@click.option("--once", is_flag=True, help="Compatibility flag; email sweep is one-shot.")
@click.option("--notify", is_flag=True, help="Send Telegram summary if new messages are found.")
@click.option("--limit", default=50, show_default=True, help="Inspect at most N latest mailbox messages.")
@click.option("--analyse-attachments", is_flag=True, help="Send full analyst read-throughs for PDF attachments.")
@click.option("--no-extract-research", is_flag=True, help="Index emails only; skip structured research extraction.")
@click.option("--provider", default=None, type=click.Choice(["gmail_api", "imap"], case_sensitive=False),
              help="Override RESEARCH_EMAIL_PROVIDER for this run.")
def email_sweep_cmd(once, notify, limit, analyse_attachments, no_extract_research, provider):
    """Sweep the configured research mailbox for research emails."""
    import os
    from scripts.email_sweep import email_sweep
    if provider:
        os.environ["RESEARCH_EMAIL_PROVIDER"] = provider
    stats = email_sweep(
        notify=notify,
        limit=limit,
        analyse_attachments=analyse_attachments,
        extract_research=not no_extract_research,
    )
    click.echo(json.dumps(stats, indent=2, ensure_ascii=False))


@cli.command("gmail-auth")
@click.option("--client-secret", default=None,
              help="Path to Google Desktop OAuth client secret JSON.")
@click.option("--token-path", default=None,
              help="Where to save the Gmail OAuth token JSON.")
@click.option("--port", default=0, show_default=True,
              help="Local callback port; 0 lets Google auth choose one.")
def gmail_auth_cmd(client_secret, token_path, port):
    """Authorize Gmail API access and save a local OAuth token."""
    from scripts.email_sweep import gmail_authorize

    try:
        stats = gmail_authorize(
            client_secret_path=client_secret,
            token_path=token_path,
            port=port,
        )
    except Exception as e:
        raise click.ClickException(str(e)) from e
    click.echo(json.dumps(stats, indent=2, ensure_ascii=False))


@cli.command("headline-sweep")
@click.option("--once", is_flag=True, help="Compatibility flag; headline sweep is one-shot.")
@click.option("--notify", is_flag=True, help="Send the ranked digest to Telegram.")
@click.option("--max-items", default=20, show_default=True,
              help="Maximum material headlines in the digest.")
@click.option("--window-hours", default=6, show_default=True,
              help="Lookback window for headline search and digest ranking.")
def headline_sweep_cmd(once, notify, max_items, window_hours):
    """Sweep configured headline sources and produce a ranked digest."""
    from scripts.headlines import headline_sweep
    stats = headline_sweep(notify=notify, max_digest_items=max_items, window_hours=window_hours)
    click.echo(json.dumps(stats, indent=2, ensure_ascii=False))


@cli.command("folder-scan")
@click.option("--folder", default=r"C:\Users\Owner\Downloads\research-inbox",
              show_default=True,
              help="Folder of PDFs to enqueue.")
@click.option("--notify", is_flag=True, help="Notify Telegram with a scan summary.")
@click.option("--recursive", is_flag=True, help="Scan nested folders too.")
@click.option("--analyse", is_flag=True, help="Send full analyst read-throughs for each queued PDF.")
def folder_scan_cmd(folder, notify, recursive, analyse):
    """Scan a folder and enqueue PDFs for ingestion by the worker."""
    from scripts.folder_scan import folder_scan
    stats = folder_scan(folder=folder, notify=notify, recursive=recursive, analyse=analyse)
    click.echo(json.dumps(stats, indent=2, ensure_ascii=False))


@cli.command("ask")
@click.argument("question", nargs=-1, required=True)
@click.option("--sources", default="all", show_default=True,
              type=click.Choice(["all", "research", "claude", "company", "headlines", "email", "skills"],
                                case_sensitive=False),
              help="Which KB source set to search.")
@click.option("--limit", default=8, show_default=True, help="Number of KB chunks to retrieve.")
def ask_cmd(question, sources, limit):
    """Ask the analyst agent using the local KB as private memory."""
    from scripts.analyst import answer_question
    text = " ".join(question).strip()
    click.echo(answer_question(text, sources=sources, limit=limit))


@cli.command("study")
@click.option("--scope", default="all", show_default=True,
              type=click.Choice(["all", "jpm"], case_sensitive=False),
              help="Research slice to study.")
@click.option("--topics", default="semiconductor,semis,memory,dram,nand,hbm,spe,wfe,ai infrastructure,electronic components",
              show_default=True,
              help="Comma-separated topic filters used to prioritize/select study targets.")
@click.option("--targets", default="",
              help="Comma-separated exact targets to study, e.g. 'ticker:MU,theme:AI Infrastructure'.")
@click.option("--since-hours", default=0.0, show_default=True, type=float,
              help="Only study targets touched by source/extraction files modified in the last N hours.")
@click.option("--max-cost", default=30.0, show_default=True, type=float,
              help="Estimated API spend ceiling in USD.")
@click.option("--limit", default=0, type=int,
              help="Study at most N targets after topic filtering (0 = no count limit).")
@click.option("--provider", default=None,
              help="Study model provider. Defaults to STUDY_PROVIDER/ANALYST_STRUCTURED_PROVIDER/ANALYST_PROVIDER.")
@click.option("--model", default=None,
              help="Study model. Defaults to STUDY_MODEL/provider-specific analyst model.")
@click.option("--max-output-tokens", default=3500, show_default=True,
              help="Reserved output-token budget per dossier.")
@click.option("--timeout", default=120.0, show_default=True,
              help="Per-model-call timeout in seconds.")
@click.option("--force", is_flag=True,
              help="Re-study targets even if a dossier already exists in study state.")
@click.option("--dry-run", is_flag=True,
              help="Plan and estimate cost without making model calls.")
@click.option("--no-embed", is_flag=True,
              help="Write dossiers and FTS index only; skip embedding creation.")
def study_cmd(scope, topics, targets, since_hours, max_cost, limit, provider, model, max_output_tokens, timeout, force, dry_run, no_embed):
    """Study the local research base and write durable company/theme dossiers."""
    import os
    from scripts.study import StudyConfig, run_study

    provider = (
        provider
        or os.environ.get("STUDY_PROVIDER")
        or os.environ.get("ANALYST_STRUCTURED_PROVIDER")
        or os.environ.get("ANALYST_PROVIDER")
        or "anthropic"
    )
    provider_l = provider.lower().strip()
    if model is None:
        if provider_l in {"openai", "gpt"}:
            model = (
                os.environ.get("STUDY_MODEL")
                or os.environ.get("ANALYST_STRUCTURED_OPENAI_MODEL")
                or os.environ.get("ANALYST_FALLBACK_MODEL")
                or "gpt-5.5"
            )
        else:
            model = (
                os.environ.get("STUDY_MODEL")
                or os.environ.get("ANALYST_STRUCTURED_ANTHROPIC_MODEL")
                or os.environ.get("ANALYST_MODEL")
                or "claude-opus-4-7"
            )
    topic_tuple = tuple(t.strip() for t in (topics or "").split(",") if t.strip())
    target_tuple = tuple(t.strip() for t in (targets or "").split(",") if t.strip())
    config = StudyConfig(
        scope=scope,
        topics=topic_tuple,
        targets=target_tuple,
        since_hours=since_hours,
        max_cost=max_cost,
        limit=limit,
        provider=provider,
        model=model,
        max_output_tokens=max_output_tokens,
        timeout=timeout,
        force=force,
        dry_run=dry_run,
        embed=not no_embed,
    )
    stats = run_study(config)
    click.echo(json.dumps(stats, indent=2, ensure_ascii=False))


@cli.command()
@click.option("--port", default=5000, help="Port to run on.")
def gui(port):
    """Launch the web GUI in the browser."""
    import webbrowser
    import threading
    from app import app

    url = f"http://localhost:{port}"
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    click.echo(f"Starting GUI at {url}")
    app.run(debug=True, port=port, use_reloader=False)


if __name__ == "__main__":
    cli()
