"""Thematic/sector research — stores industry-level research notes, builds
theme summaries, and cross-references against single-name stock research."""

import json
import re
import shutil
import statistics
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic

from scripts.fileio import extract_file_text, write_json_atomic
from scripts.llm_provider import cached_document_block, call_api, parse_json_loose
from scripts.analysis_report import (
    ANALYSIS_REPORT_INSTRUCTIONS as ANALYSIS_REPORT_ADDENDUM,
    build_second_pass_prompt,
    merge_analysis_report,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
THEMATIC_DIR = DATA_DIR / "Thematic"
CONFIG_PATH = PROJECT_ROOT / "config" / "companies.json"

RESEARCH_MODEL = "claude-opus-4-7"   # default
EXTRACTION_MODEL = "claude-sonnet-4-6"
SYNTHESIS_MODEL = "claude-opus-4-7"
MAX_TEXT_CHARS = 30_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_companies():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _ensure_theme_dirs(theme):
    base = THEMATIC_DIR / theme
    (base / "notes").mkdir(parents=True, exist_ok=True)
    (base / "analyses").mkdir(parents=True, exist_ok=True)
    return base


def _today_prefix():
    return datetime.now().strftime("%Y-%m-%d")


def _load_theme_config(theme):
    path = THEMATIC_DIR / theme / "linked_tickers.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def _save_theme_config(theme, config):
    path = THEMATIC_DIR / theme / "linked_tickers.json"
    with open(path, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _extract_file_text(path):
    return extract_file_text(path, max_chars=MAX_TEXT_CHARS)


def _call_api(client, messages, max_tokens=8192, system=None, return_response=False, model=None):
    return call_api(client, messages, max_tokens=max_tokens, system=system,
                    return_response=return_response, model=model,
                    default_model=RESEARCH_MODEL)


_parse_json_response = parse_json_loose


def _list_all_themes():
    if not THEMATIC_DIR.exists():
        return []
    return sorted([
        d.name for d in THEMATIC_DIR.iterdir()
        if d.is_dir() and (d / "linked_tickers.json").exists()
    ])


# ---------------------------------------------------------------------------
# create-theme
# ---------------------------------------------------------------------------

def create_theme():
    import click

    name = click.prompt("Theme name (e.g. WFE, HBM, CPO)")
    theme_dir = THEMATIC_DIR / name
    if (theme_dir / "linked_tickers.json").exists():
        if not click.confirm(f"Theme '{name}' already exists. Overwrite config?"):
            click.echo("Aborted.")
            return
    _ensure_theme_dirs(name)

    description = click.prompt("Description")

    click.echo("\nAdd linked tickers (type 'done' to finish):")
    companies = _load_companies()
    linked = []
    while True:
        ticker = click.prompt("  Ticker", default="done", show_default=False)
        if ticker.lower() == "done":
            break
        if ticker not in companies:
            click.echo(f"    Warning: '{ticker}' not in companies.json (will still link)")
        relevance = click.prompt(f"  Relevance of {ticker}")
        linked.append({"ticker": ticker, "relevance": relevance})

    click.echo("\nAdd key metrics to track (type 'done' to finish):")
    metrics = []
    while True:
        metric = click.prompt("  Metric", default="done", show_default=False)
        if metric.lower() == "done":
            break
        metrics.append(metric)

    config = {
        "theme": name,
        "description": description,
        "linked_tickers": linked,
        "key_metrics": metrics,
    }
    _save_theme_config(name, config)
    click.echo(f"\nCreated theme '{name}' with {len(linked)} linked tickers and {len(metrics)} key metrics.")


# ---------------------------------------------------------------------------
# edit-theme
# ---------------------------------------------------------------------------

def edit_theme(theme):
    import click

    config = _load_theme_config(theme)
    if not config:
        click.echo(f"Theme '{theme}' not found.")
        return

    click.echo(f"Editing theme: {theme}")
    click.echo(f"Description: {config['description']}")

    if click.confirm("\nEdit description?", default=False):
        config["description"] = click.prompt("New description", default=config["description"])

    # Edit linked tickers
    click.echo(f"\nCurrent linked tickers ({len(config.get('linked_tickers', []))}):")
    for lt in config.get("linked_tickers", []):
        click.echo(f"  {lt['ticker']}: {lt['relevance']}")

    if click.confirm("\nAdd more tickers?", default=False):
        companies = _load_companies()
        while True:
            ticker = click.prompt("  Ticker", default="done", show_default=False)
            if ticker.lower() == "done":
                break
            # Check for duplicates
            existing = [lt["ticker"] for lt in config.get("linked_tickers", [])]
            if ticker in existing:
                click.echo(f"    '{ticker}' already linked, skipping.")
                continue
            if ticker not in companies:
                click.echo(f"    Warning: '{ticker}' not in companies.json")
            relevance = click.prompt(f"  Relevance of {ticker}")
            config.setdefault("linked_tickers", []).append(
                {"ticker": ticker, "relevance": relevance}
            )

    if click.confirm("Remove any tickers?", default=False):
        to_remove = click.prompt("Tickers to remove (comma-separated)")
        remove_set = {t.strip() for t in to_remove.split(",")}
        config["linked_tickers"] = [
            lt for lt in config.get("linked_tickers", [])
            if lt["ticker"] not in remove_set
        ]
        click.echo(f"  Removed: {remove_set}")

    # Edit key metrics
    click.echo(f"\nCurrent key metrics ({len(config.get('key_metrics', []))}):")
    for m in config.get("key_metrics", []):
        click.echo(f"  - {m}")

    if click.confirm("\nAdd more metrics?", default=False):
        while True:
            metric = click.prompt("  Metric", default="done", show_default=False)
            if metric.lower() == "done":
                break
            config.setdefault("key_metrics", []).append(metric)

    if click.confirm("Remove any metrics?", default=False):
        to_remove = click.prompt("Metrics to remove (comma-separated)")
        remove_set = {m.strip() for m in to_remove.split(",")}
        config["key_metrics"] = [
            m for m in config.get("key_metrics", [])
            if m not in remove_set
        ]

    _save_theme_config(theme, config)
    click.echo(f"\nSaved updated config for '{theme}'.")


# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------

def _build_thematic_extraction_prompt(config):
    theme_name = config["theme"]
    description = config["description"]

    tickers_text = "\n".join(
        f"- {lt['ticker']}: {lt['relevance']}"
        for lt in config.get("linked_tickers", [])
    )
    metrics_text = ", ".join(config.get("key_metrics", []))

    return f"""\
You are a buy-side equity research analyst at a macro hedge fund. I'm giving you a thematic/sector research document about {theme_name}: {description}.

The companies most relevant to this theme are:
{tickers_text}

The key metrics to track for this theme are: {metrics_text}

Analyse this document and respond with ONLY a JSON object (no markdown, no preamble):

{{
  "metadata": {{
    "source": "analyst/firm",
    "date": "YYYY-MM-DD",
    "title": "...",
    "author": "..."
  }},
  "theme_estimates": {{
    "metric_name": {{
      "CY2024": {{"value": "...", "source_detail": "..."}},
      "CY2025": {{"value": "...", "source_detail": "..."}},
      "CY2026": {{"value": "...", "source_detail": "..."}},
      "CY2027": {{"value": "...", "source_detail": "..."}}
    }}
  }},
  "company_specific_mentions": {{
    "TICKER": {{
      "mentions": ["direct quotes or paraphrased mentions of this company"],
      "implied_estimates": {{
        "revenue_from_theme": {{"CY2025": "...", "CY2026": "..."}},
        "market_share": "...",
        "other": {{}}
      }},
      "outlook": "positive/negative/neutral/mixed",
      "key_points": ["..."]
    }}
  }},
  "industry_dynamics": {{
    "demand_drivers": ["..."],
    "supply_dynamics": ["..."],
    "pricing_trends": "...",
    "technology_shifts": ["..."],
    "geographic_trends": {{"China": "...", "US": "...", "Other": "..."}},
    "cyclical_position": "early_cycle/mid_cycle/late_cycle/downturn/recovery"
  }},
  "key_debates": [
    {{
      "debate": "...",
      "bull_case": "...",
      "bear_case": "...",
      "author_lean": "bull/bear/neutral"
    }}
  ],
  "detailed_summary": "Comprehensive 500-1000 word summary...",
  "analysis_report": "SEE INSTRUCTIONS BELOW"
}}

For companies not mentioned in the document, omit them from company_specific_mentions.
For metrics not available, use null. Extract as many specific numbers as possible.
""" + ANALYSIS_REPORT_ADDENDUM + """
The research document is provided between <document> tags in the system context. Extract the structured JSON only.
"""


# ---------------------------------------------------------------------------
# Source label helper (reuse pattern from research.py)
# ---------------------------------------------------------------------------

def _source_label(meta):
    source = meta.get("source", "Unknown")
    author = meta.get("author")
    firm = source
    if "/" in firm:
        parts = [p.strip() for p in firm.split("/")]
        candidates = [p for p in parts if "," not in p and len(p.split()) <= 4]
        if candidates:
            generic = {"global markets research", "equity research", "research"}
            non_generic = [p for p in candidates if p.lower() not in generic]
            firm = non_generic[0] if non_generic else candidates[0]
        else:
            firm = parts[0]
    if author and author.lower() not in ("null", "n/a", ""):
        first_author = author.split(",")[0].strip()
        if first_author.lower() != firm.lower():
            return f"{firm} ({first_author})"
    return firm


# ---------------------------------------------------------------------------
# store-thematic
# ---------------------------------------------------------------------------

def store_thematic(theme, file_paths):
    import click

    config = _load_theme_config(theme)
    if not config:
        click.echo(f"Theme '{theme}' not found. Run: python main.py create-theme")
        raise SystemExit(1)

    theme_dir = _ensure_theme_dirs(theme)
    notes_dir = theme_dir / "notes"
    client = Anthropic(max_retries=3, timeout=600.0)

    for i, file_path in enumerate(file_paths):
        if i > 0:
            click.echo(f"\n{'='*60}\n")

        src = Path(file_path)
        if not src.exists():
            click.echo(f"File not found: {file_path}")
            continue

        # a. Copy file
        dest_name = f"{_today_prefix()}_{src.name}"
        dest = notes_dir / dest_name
        shutil.copy2(src, dest)
        click.echo(f"Copied to {dest.relative_to(PROJECT_ROOT)}")

        # b. Extract text
        click.echo(f"Extracting text from {src.name}...")
        text = _extract_file_text(src)
        click.echo(f"  Extracted {len(text):,} characters")

        # c. Send to Claude
        click.echo("Analysing with Claude API...")
        prompt = _build_thematic_extraction_prompt(config)
        raw = _call_api(client, [{"role": "user", "content": prompt}], max_tokens=16384,
                        model=EXTRACTION_MODEL, system=cached_document_block(text))

        # d. Parse and save
        note_data = _parse_json_response(raw)
        json_path = notes_dir / f"{dest_name}.json"
        with open(json_path, "w") as f:
            json.dump(note_data, f, indent=2, ensure_ascii=False)
            f.write("\n")

        meta = note_data.get("metadata", {})
        click.echo(f"Saved: {json_path.relative_to(PROJECT_ROOT)}")
        click.echo(f"  Source: {meta.get('source', 'Unknown')}")
        click.echo(f"  Date:   {meta.get('date', 'Unknown')}")
        click.echo(f"  Title:  {meta.get('title', 'Unknown')}")

        # Second pass: View Evolution & Cross-Author Comparison
        click.echo("\nGenerating view evolution & comparison...")
        summary_path = theme_dir / "theme_summary.json"
        existing_summary = None
        if summary_path.exists():
            with open(summary_path, encoding="utf-8") as f:
                existing_summary = json.load(f)

        # For thematic: source history = their entries in theme_summary
        source_label = _source_label(meta)
        source_history = None
        if existing_summary:
            for src_entry in existing_summary.get("sources", []):
                if src_entry.get("source") == source_label:
                    source_history = src_entry
                    break

        second_prompt = build_second_pass_prompt(
            new_note_json=note_data,
            author_history_json=source_history,
            summary_json=existing_summary,
        )
        view_evolution = _call_api(client, [{"role": "user", "content": second_prompt}], max_tokens=16384, model=SYNTHESIS_MODEL)
        report = merge_analysis_report(note_data, view_evolution)

        # Re-save JSON with complete analysis_report
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(note_data, f, indent=2, ensure_ascii=False)
            f.write("\n")

        # Print and save
        click.echo("")
        click.echo(report)

        summary_md_path = notes_dir / f"{dest_name}_summary.md"
        with open(summary_md_path, "w", encoding="utf-8") as f:
            f.write(report)
        click.echo(f"\nSaved summary: {summary_md_path.relative_to(PROJECT_ROOT)}")

        # Log unlisted company mentions (non-interactive)
        linked_tickers = {lt["ticker"] for lt in config.get("linked_tickers", [])}
        mentions = note_data.get("company_specific_mentions", {})
        unlisted = [t for t in mentions if t not in linked_tickers]
        if unlisted:
            click.echo(f"  Note: document mentions unlisted tickers: {', '.join(unlisted)}")
            click.echo(f"  Run 'python main.py edit-theme \"{theme}\"' to add them.")

    # e. Rebuild summary
    click.echo("\nRebuilding theme summary...")
    rebuild_theme_summary(theme)
    click.echo("Done.")


# ---------------------------------------------------------------------------
# Theme summary builder
# ---------------------------------------------------------------------------

def _numeric_val(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        cleaned = v.replace(",", "").replace("$", "").replace("%", "").replace("B", "").replace("b", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def rebuild_theme_summary(theme):
    import click

    config = _load_theme_config(theme)
    if not config:
        click.echo(f"Theme '{theme}' not found.")
        return

    theme_dir = _ensure_theme_dirs(theme)
    notes_dir = theme_dir / "notes"

    note_files = sorted(notes_dir.glob("*.json"))
    if not note_files:
        click.echo("  No thematic notes found — skipping summary build.")
        return

    notes = []
    for nf in note_files:
        try:
            with open(nf) as f:
                notes.append((nf.name, json.load(f)))
        except Exception as e:
            click.echo(f"  Warning: skipping {nf.name}: {e}")

    if not notes:
        return

    # Build sources
    sources = []
    for filename, note in notes:
        meta = note.get("metadata", {})
        original_file = filename.removesuffix(".json")
        sources.append({
            "source": _source_label(meta),
            "source_type": meta.get("source_type", "thematic"),
            "date": meta.get("date"),
            "file": original_file,
        })

    # Build theme estimates consensus
    CY_KEYS = ["CY2024", "CY2025", "CY2026", "CY2027"]
    theme_estimates = {}  # metric -> cy -> { sellside: {source: {value, date}}, mean, high, low }

    for _, note in notes:
        meta = note.get("metadata", {})
        source_label = _source_label(meta)
        date = meta.get("date")

        for metric, periods in note.get("theme_estimates", {}).items():
            if not isinstance(periods, dict):
                continue
            if metric not in theme_estimates:
                theme_estimates[metric] = {}
            for cy in CY_KEYS:
                cy_data = periods.get(cy, {})
                if not isinstance(cy_data, dict):
                    continue
                raw_val = cy_data.get("value")
                # Store the raw string value for display, numeric for stats
                if raw_val is None:
                    continue
                if cy not in theme_estimates[metric]:
                    theme_estimates[metric][cy] = {"sellside": {}}
                theme_estimates[metric][cy]["sellside"][source_label] = {
                    "value": raw_val,
                    "date": date,
                    "source_detail": cy_data.get("source_detail", ""),
                }

    # Compute mean/high/low for numeric metrics
    for metric in theme_estimates:
        for cy in CY_KEYS:
            if cy not in theme_estimates[metric]:
                continue
            bucket = theme_estimates[metric][cy]
            nums = []
            for src_data in bucket["sellside"].values():
                n = _numeric_val(src_data["value"])
                if n is not None:
                    nums.append(n)
            bucket["mean"] = round(statistics.mean(nums), 2) if nums else None
            bucket["high"] = max(nums) if nums else None
            bucket["low"] = min(nums) if nums else None

    # Build company mentions aggregate
    company_mentions = {}  # ticker -> list of {source, outlook, key_points, implied_estimates}
    for _, note in notes:
        meta = note.get("metadata", {})
        source_label = _source_label(meta)
        for ticker, data in note.get("company_specific_mentions", {}).items():
            if not isinstance(data, dict):
                continue
            if ticker not in company_mentions:
                company_mentions[ticker] = []
            company_mentions[ticker].append({
                "source": source_label,
                "outlook": data.get("outlook"),
                "key_points": data.get("key_points", []),
                "implied_estimates": data.get("implied_estimates", {}),
                "market_share": data.get("implied_estimates", {}).get("market_share"),
            })

    # Key debates aggregate
    all_debates = []
    for _, note in notes:
        meta = note.get("metadata", {})
        source_label = _source_label(meta)
        for debate in note.get("key_debates", []):
            if isinstance(debate, dict):
                debate["source"] = source_label
                all_debates.append(debate)

    # Assemble summary
    summary = {
        "theme": theme,
        "description": config["description"],
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "sources_count": len(sources),
        "sources": sources,
        "theme_estimates": theme_estimates,
        "company_mentions": company_mentions,
        "key_debates": all_debates,
        "linked_tickers": config.get("linked_tickers", []),
        "key_metrics": config.get("key_metrics", []),
    }

    summary_json_path = theme_dir / "theme_summary.json"
    write_json_atomic(summary_json_path, summary)
    click.echo(f"  Saved {summary_json_path.relative_to(PROJECT_ROOT)}")

    md = _generate_theme_summary_md(summary)
    summary_md_path = theme_dir / "theme_summary.md"
    with open(summary_md_path, "w", encoding="utf-8") as f:
        f.write(md)
    click.echo(f"  Saved {summary_md_path.relative_to(PROJECT_ROOT)}")


def _generate_theme_summary_md(summary):
    theme = summary["theme"]
    desc = summary["description"]
    updated = summary["last_updated"][:10]
    n = summary["sources_count"]

    CY_KEYS = ["CY2024", "CY2025", "CY2026", "CY2027"]

    lines = [
        f"# {theme} — Thematic Research Summary",
        f"{desc}",
        f"Last updated: {updated} | Sources: {n}",
        "",
    ]

    # Industry Estimates table
    te = summary.get("theme_estimates", {})
    if te:
        lines.append("## Industry Estimates")
        lines.append("")
        lines.append(f"| Metric | CY2024 | CY2025E | CY2026E | CY2027E |")
        lines.append(f"|--------|--------|---------|---------|---------|")

        for metric, periods in te.items():
            lines.append(f"| **{metric}** | | | | |")

            # Collect all sources for this metric
            all_sources = set()
            for cy in CY_KEYS:
                if cy in periods:
                    all_sources |= set(periods[cy].get("sellside", {}).keys())

            for src in sorted(all_sources):
                vals = []
                for cy in CY_KEYS:
                    ss = periods.get(cy, {}).get("sellside", {}).get(src, {})
                    v = ss.get("value", "")
                    vals.append(str(v) if v else "N/A")
                lines.append(f"| {src} | {' | '.join(vals)} |")

            # Mean row
            vals = []
            for cy in CY_KEYS:
                m = periods.get(cy, {}).get("mean")
                vals.append(f"{m:,.1f}" if m is not None else "N/A")
            lines.append(f"| Mean | {' | '.join(vals)} |")

        lines.append("")

    # Company Implications table
    cm = summary.get("company_mentions", {})
    companies = _load_companies()
    if cm:
        lines.append("## Company Implications")
        lines.append("")
        lines.append("| Company | Theme Relevance | Latest View | Key Points |")
        lines.append("|---------|----------------|-------------|------------|")

        # Build relevance map from linked tickers
        relevance_map = {}
        for lt in summary.get("linked_tickers", []):
            relevance_map[lt["ticker"]] = lt["relevance"]

        for ticker in sorted(cm.keys()):
            mentions = cm[ticker]
            name = companies.get(ticker, {}).get("name", ticker)
            relevance = relevance_map.get(ticker, "")
            # Use most recent mention
            latest = mentions[-1]
            outlook = latest.get("outlook", "N/A")
            src = latest.get("source", "")
            kps = latest.get("key_points", [])
            kp_str = "; ".join(kps[:2]) if kps else "—"
            lines.append(
                f"| {ticker} ({name}) | {relevance} | {src}: {outlook} | {kp_str} |"
            )

        lines.append("")

    # Key Debates
    debates = summary.get("key_debates", [])
    if debates:
        lines.append("## Key Debates")
        lines.append("")
        for i, d in enumerate(debates, 1):
            lines.append(f"{i}. **{d.get('debate', '?')}**")
            lines.append(f"   - Bull: {d.get('bull_case', 'N/A')}")
            lines.append(f"   - Bear: {d.get('bear_case', 'N/A')}")
            lines.append(f"   - Author leans: {d.get('author_lean', 'N/A')} ({d.get('source', '')})")
        lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# analyse-thematic
# ---------------------------------------------------------------------------

ANALYSE_THEMATIC_PROMPT = """\
You are a buy-side equity research analyst at a macro hedge fund. I'm providing:

1. NEW THEMATIC RESEARCH: A new research note about {theme_name}
2. EXISTING THEMATIC SUMMARY: Previous research views on {theme_name}
3. LINKED COMPANY RESEARCH: Single-name research summaries for companies exposed to this theme

Provide an extremely detailed analysis:

## A. DETAILED SUMMARY OF NEW RESEARCH
Comprehensive summary of the new thematic note with all specific numbers and data points.

## B. THEMATIC ESTIMATE COMPARISON TABLE
Compare the new note's industry-level estimates against all existing thematic research:
| Metric | New (Source) | Existing Mean | vs Mean | ... |
For each key metric and each forecast year.

## C. CROSS-REFERENCE: THEME vs SINGLE-NAME RESEARCH

This is the critical section. For each linked company that has stored research:

### {{TICKER}} — {{Company Name}}
- **Theme implies**: Based on the thematic estimates and the company's market share/exposure
- **Single-name sellside estimates**: What the single-name research says
- **Gap**: Quantify the difference
- **Possible explanations**: Why the gap exists

Create a summary table:
| Company | Theme-Implied Rev | Single-Name Consensus Rev | Gap | Gap % | Notes |

## D. KEY ALIGNMENTS
Where the thematic view and single-name views agree.

## E. KEY DIVERGENCES
Where they don't — and WHY. This is where alpha lives.

## F. INVESTMENT IMPLICATIONS
- Which single-name stocks look mispriced relative to the thematic view?
- Where are the biggest gaps between theme-implied and single-name estimates?
- What would need to be true to close each gap?

Be extremely detailed and specific throughout. Use actual numbers, not vague descriptions.
"""


def analyse_thematic(theme, file_path):
    import click

    config = _load_theme_config(theme)
    if not config:
        click.echo(f"Theme '{theme}' not found. Run: python main.py create-theme")
        raise SystemExit(1)

    theme_dir = _ensure_theme_dirs(theme)
    summary_path = theme_dir / "theme_summary.json"

    # Load existing theme summary
    existing_summary = None
    if summary_path.exists():
        with open(summary_path) as f:
            existing_summary = json.load(f)

    src = Path(file_path)
    if not src.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    client = Anthropic(max_retries=3, timeout=600.0)

    # a. Extract and analyse the new note
    click.echo(f"Extracting text from {src.name}...")
    text = _extract_file_text(src)
    click.echo(f"  Extracted {len(text):,} characters")

    click.echo("Extracting structured data from thematic note...")
    extraction_prompt = _build_thematic_extraction_prompt(config)
    raw_extraction = _call_api(client, [{"role": "user", "content": extraction_prompt}], max_tokens=16384,
                               model=EXTRACTION_MODEL, system=cached_document_block(text))
    new_note = _parse_json_response(raw_extraction)

    # b. Load theme summary
    # c. Load single-name summaries for linked tickers
    company_summaries = {}
    for lt in config.get("linked_tickers", []):
        ticker = lt["ticker"]
        sn_path = DATA_DIR / ticker / "research" / "summary.json"
        if sn_path.exists():
            with open(sn_path) as f:
                company_summaries[ticker] = json.load(f)

    # Build the analysis prompt
    theme_name = config["theme"]
    prompt = ANALYSE_THEMATIC_PROMPT.format(theme_name=theme_name)

    prompt += f"\n\n--- NEW THEMATIC RESEARCH (structured extraction) ---\n{json.dumps(new_note, indent=2)}\n"

    if existing_summary:
        prompt += f"\n--- EXISTING THEMATIC SUMMARY ---\n{json.dumps(existing_summary, indent=2)}\n"
    else:
        prompt += f"\n--- EXISTING THEMATIC SUMMARY ---\nNo existing thematic research stored for {theme_name}.\n"

    prompt += "\n--- LINKED COMPANY RESEARCH ---\n"
    companies = _load_companies()
    for lt in config.get("linked_tickers", []):
        ticker = lt["ticker"]
        name = companies.get(ticker, {}).get("name", ticker)
        if ticker in company_summaries:
            prompt += f"\n### {ticker} ({name}) — Single-Name Research Summary:\n"
            prompt += json.dumps(company_summaries[ticker], indent=2) + "\n"
        else:
            prompt += f"\n### {ticker} ({name}): No single-name research stored.\n"

    # Raw text rides in a cached system block so cross-cut synthesis calls for
    # the same doc share one cache entry.
    prompt += "\nThe raw text of the new thematic research is provided between <document> tags in the system context, for additional detail.\n"

    click.echo("Running cross-reference analysis...")
    analysis = _call_api(client, [{"role": "user", "content": prompt}], max_tokens=16384,
                         model=SYNTHESIS_MODEL, system=cached_document_block(text[:15000]))

    # e. Print
    click.echo("")
    click.echo(analysis)

    # f. Save analysis
    analyses_dir = theme_dir / "analyses"
    analyses_dir.mkdir(parents=True, exist_ok=True)
    analysis_filename = f"{_today_prefix()}_{src.stem}_analysis.md"
    analysis_path = analyses_dir / analysis_filename
    with open(analysis_path, "w", encoding="utf-8") as f:
        f.write(analysis)
    click.echo(f"\nSaved analysis: {analysis_path.relative_to(PROJECT_ROOT)}")

    # g. Ask to store
    if click.confirm("\nWould you like to add this to the stored thematic research base?"):
        notes_dir = theme_dir / "notes"
        dest_name = f"{_today_prefix()}_{src.name}"
        dest = notes_dir / dest_name
        shutil.copy2(src, dest)
        click.echo(f"Copied to {dest.relative_to(PROJECT_ROOT)}")

        json_path = notes_dir / f"{dest_name}.json"
        with open(json_path, "w") as f:
            json.dump(new_note, f, indent=2, ensure_ascii=False)
            f.write("\n")
        click.echo(f"Saved extraction: {json_path.relative_to(PROJECT_ROOT)}")

        click.echo("Rebuilding theme summary...")
        rebuild_theme_summary(theme)
        click.echo("Done.")
    else:
        click.echo("Research not stored.")


# ---------------------------------------------------------------------------
# show / list
# ---------------------------------------------------------------------------

def show_theme_summary(theme):
    import click

    md_path = THEMATIC_DIR / theme / "theme_summary.md"
    if not md_path.exists():
        click.echo(f"No thematic summary found for '{theme}'.")
        click.echo(f"Run: python main.py store-thematic \"{theme}\" file1.pdf")
        return
    click.echo(md_path.read_text(encoding="utf-8"))


def list_themes():
    import click

    themes = _list_all_themes()
    if not themes:
        click.echo("No themes created yet.")
        click.echo("Run: python main.py create-theme")
        return

    companies = _load_companies()
    for theme in themes:
        config = _load_theme_config(theme)
        desc = config.get("description", "")
        tickers = [lt["ticker"] for lt in config.get("linked_tickers", [])]
        n_metrics = len(config.get("key_metrics", []))

        # Count notes
        notes_dir = THEMATIC_DIR / theme / "notes"
        n_notes = len(list(notes_dir.glob("*.json"))) if notes_dir.exists() else 0

        click.echo(f"\n{theme}: {desc}")
        click.echo(f"  Linked tickers: {', '.join(tickers) if tickers else 'none'}")
        click.echo(f"  Key metrics: {n_metrics} | Notes stored: {n_notes}")
