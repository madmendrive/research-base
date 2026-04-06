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
