"""Macro research storage and analysis — stores macro/strategy research by author,
tracks view evolution over time, and provides cross-author comparison."""

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic

from scripts.classifier import extract_text
from scripts.analysis_report import (
    ANALYSIS_REPORT_INSTRUCTIONS as ANALYSIS_REPORT_ADDENDUM,
    build_second_pass_prompt,
    merge_analysis_report,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
MACRO_DIR = DATA_DIR / "Macro"

RESEARCH_MODEL = "claude-opus-4-7"
MAX_TEXT_CHARS = 30_000

VIEW_TOPICS = [
    "growth_outlook", "inflation_outlook", "monetary_policy",
    "rates_and_yields", "fx_outlook", "equity_outlook",
    "commodities", "geopolitics", "flows_and_positioning",
]

THEME_CATEGORIES = [
    "fed_policy", "china_macro", "geopolitics", "rates_and_yields",
    "cross_asset_flows", "equity_strategy", "other",
]

MAX_HISTORY = 50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_dirs(author):
    base = MACRO_DIR / "authors" / author / "notes"
    base.mkdir(parents=True, exist_ok=True)
    (MACRO_DIR / "themes").mkdir(parents=True, exist_ok=True)
    (MACRO_DIR / "analyses").mkdir(parents=True, exist_ok=True)
    return MACRO_DIR / "authors" / author


def _today_prefix():
    return datetime.now().strftime("%Y-%m-%d")


def _extract_file_text(path):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".pdf", ".htm", ".html"):
        text, err = extract_text(path, max_pages=200, max_chars=MAX_TEXT_CHARS)
        if err:
            raise RuntimeError(f"Failed to extract text from {path.name}: {err}")
        if not text:
            raise RuntimeError(f"No text extracted from {path.name}")
        return text
    elif suffix in (".txt", ".md", ".csv"):
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS] + "\n[...truncated]"
        return text
    else:
        raise RuntimeError(f"Unsupported file type: {suffix}")


def _call_api(client, messages, max_tokens=8192):
    import time
    for attempt in range(3):
        try:
            response = client.messages.create(
                model=RESEARCH_MODEL,
                max_tokens=max_tokens,
                messages=messages,
            )
            return response.content[0].text
        except Exception as e:
            err_str = str(e).lower()
            if "overloaded" in err_str or "connection" in err_str or "529" in err_str or "disconnected" in err_str:
                if attempt < 2:
                    wait = 5 * (attempt + 1)
                    print(f"  API transient error, retrying in {wait}s (streaming)...")
                    time.sleep(wait)
                    # Fall back to streaming to keep connection alive
                    try:
                        chunks = []
                        with client.messages.stream(
                            model=RESEARCH_MODEL,
                            max_tokens=max_tokens,
                            messages=messages,
                        ) as stream:
                            for text in stream.text_stream:
                                chunks.append(text)
                            final = stream.get_final_message()
                        # If the stream was truncated (max_tokens hit, connection drop),
                        # don't return partial chunks — fall through to next retry.
                        if final.stop_reason not in ("end_turn", "stop_sequence"):
                            print(f"  stream stop_reason={final.stop_reason!r}, retrying...")
                            continue
                        return "".join(chunks)
                    except Exception as inner:
                        print(f"  streaming retry failed: {inner}, trying again...")
                        continue
            raise
    raise RuntimeError("API call failed after 3 attempts")


def _parse_json_response(text):
    text = text.strip()
    md_match = re.match(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if md_match:
        text = md_match.group(1).strip()
    return json.loads(text)


def _list_all_authors():
    authors_dir = MACRO_DIR / "authors"
    if not authors_dir.exists():
        return []
    return sorted([
        d.name for d in authors_dir.iterdir()
        if d.is_dir() and (d / "notes").exists()
    ])


# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------

def _build_extraction_prompt(author):
    return f"""\
You are a macro research analyst at a global macro hedge fund. I'm giving you a macro research document by {author}.

Analyse this document and respond with ONLY a JSON object (no markdown, no preamble):

{{
  "metadata": {{
    "author": "...",
    "firm": "...",
    "date": "YYYY-MM-DD",
    "title": "...",
    "publication_type": "weekly_note" | "thematic_report" | "strategy_update" | "data_report" | "other"
  }},
  "macro_views": {{
    "growth_outlook": {{
      "view": "detailed description",
      "regions": {{
        "US": {{"growth_forecast": "x%", "direction": "accelerating/decelerating/stable"}},
        "Europe": {{"growth_forecast": "x%", "direction": "..."}},
        "China": {{"growth_forecast": "x%", "direction": "..."}},
        "Global": {{"growth_forecast": "x%", "direction": "..."}}
      }},
      "sentiment": "positive/negative/neutral/mixed"
    }},
    "inflation_outlook": {{
      "view": "...",
      "us_cpi_forecast": "x%",
      "direction": "rising/falling/sticky",
      "sentiment": "..."
    }},
    "monetary_policy": {{
      "view": "...",
      "fed_funds_forecast": "x%",
      "rate_path": "description of expected rate path",
      "qt_view": "...",
      "sentiment": "hawkish/dovish/neutral"
    }},
    "rates_and_yields": {{
      "view": "...",
      "us_10y_forecast": "x%",
      "yield_curve_view": "steepening/flattening/stable",
      "credit_spreads_view": "..."
    }},
    "fx_outlook": {{
      "view": "...",
      "dxy_direction": "strengthening/weakening/range-bound",
      "key_pairs": {{"EURUSD": "...", "USDJPY": "...", "USDCNY": "..."}}
    }},
    "equity_outlook": {{
      "view": "...",
      "sp500_target": number_or_null,
      "sector_preferences": ["..."],
      "style_preference": "growth/value/quality/defensive",
      "positioning_view": "..."
    }},
    "commodities": {{
      "oil_view": "...",
      "oil_price_forecast": number_or_null,
      "gold_view": "...",
      "other": "..."
    }},
    "geopolitics": {{
      "key_risks": ["..."],
      "view": "..."
    }},
    "flows_and_positioning": {{
      "fund_flows": "description of recent flow trends",
      "positioning": "description of current market positioning",
      "sentiment_indicators": "..."
    }}
  }},
  "themes": [
    {{
      "theme": "e.g. US fiscal deficit trajectory",
      "category": "fed_policy" | "china_macro" | "geopolitics" | "rates_and_yields" | "cross_asset_flows" | "equity_strategy" | "other",
      "detailed_view": "...",
      "trades_or_implications": ["..."]
    }}
  ],
  "key_data_points": [
    {{"data_point": "e.g. US ISM Manufacturing", "value": "52.3", "context": "above consensus of 51.0"}}
  ],
  "recommended_trades_or_positioning": [
    {{"trade": "Long 10Y UST", "rationale": "..."}}
  ],
  "detailed_summary": "Comprehensive 500-1000 word summary covering all key arguments, data points, and conclusions. Be extremely specific with numbers.",
  "analysis_report": "SEE INSTRUCTIONS BELOW"
}}

For any field where the information is not available in the document, use null. Extract as many specific numbers, percentages, and data points as possible.
""" + ANALYSIS_REPORT_ADDENDUM + """

Document text:
"""


# ---------------------------------------------------------------------------
# Author summary with view history
# ---------------------------------------------------------------------------

def _load_author_summary(author_dir):
    path = author_dir / "author_summary.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {
        "author": author_dir.name,
        "notes_count": 0,
        "latest_note_date": None,
        "views": {},
        "recommended_trades_history": [],
    }


def _save_author_summary(author_dir, summary):
    path = author_dir / "author_summary.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _update_author_views(summary, note_data, source_filename):
    """Update author summary with views from a new note."""
    meta = note_data.get("metadata", {})
    note_date = meta.get("date") or _today_prefix()
    macro_views = note_data.get("macro_views", {})

    for topic in VIEW_TOPICS:
        topic_data = macro_views.get(topic)
        if not topic_data or not isinstance(topic_data, dict):
            continue

        view_text = topic_data.get("view")
        if not view_text:
            continue

        sentiment = topic_data.get("sentiment") or topic_data.get("direction") or ""

        new_entry = {
            "date": note_date,
            "view": view_text,
            "sentiment": sentiment,
            "source_note": source_filename,
        }

        if topic not in summary["views"]:
            summary["views"][topic] = {"current": new_entry, "history": []}
        else:
            existing = summary["views"][topic]
            current = existing.get("current")

            if current:
                # Move current to history (always track the progression)
                existing["history"].insert(0, current)
                # Cap history
                existing["history"] = existing["history"][:MAX_HISTORY]

            existing["current"] = new_entry

    # Update trades history
    trades = note_data.get("recommended_trades_or_positioning", [])
    if trades:
        summary["recommended_trades_history"].insert(0, {
            "date": note_date,
            "trades": trades,
        })
        summary["recommended_trades_history"] = summary["recommended_trades_history"][:MAX_HISTORY]

    # Update counts
    notes_dir = Path(summary.get("_author_dir", "")) / "notes" if "_author_dir" in summary else None
    summary["latest_note_date"] = note_date


def _rebuild_author_summary(author_dir):
    """Rebuild author summary from all stored notes."""
    notes_dir = author_dir / "notes"
    note_files = sorted(notes_dir.glob("*.json"))

    summary = {
        "author": author_dir.name,
        "notes_count": len(note_files),
        "latest_note_date": None,
        "views": {},
        "recommended_trades_history": [],
    }

    # Process notes in chronological order (oldest first)
    for nf in note_files:
        try:
            with open(nf, encoding="utf-8") as f:
                note_data = json.load(f)
        except Exception:
            continue

        source_filename = nf.name.removesuffix(".json")
        _update_author_views(summary, note_data, source_filename)

    if note_files:
        # Get latest date from the last note
        try:
            with open(note_files[-1], encoding="utf-8") as f:
                last = json.load(f)
            summary["latest_note_date"] = last.get("metadata", {}).get("date") or _today_prefix()
        except Exception:
            summary["latest_note_date"] = _today_prefix()

    _save_author_summary(author_dir, summary)
    return summary


# ---------------------------------------------------------------------------
# Theme tracking
# ---------------------------------------------------------------------------

def _update_themes(note_data, author):
    """Update theme files with views from a new note."""
    themes = note_data.get("themes", [])
    if not themes:
        return

    meta = note_data.get("metadata", {})
    note_date = meta.get("date") or _today_prefix()
    themes_dir = MACRO_DIR / "themes"
    themes_dir.mkdir(parents=True, exist_ok=True)

    for theme_entry in themes:
        if not isinstance(theme_entry, dict):
            continue
        category = theme_entry.get("category", "other")
        if category not in THEME_CATEGORIES:
            category = "other"

        theme_file = themes_dir / f"{category}.json"
        theme_data = {"theme": category, "views": []}
        if theme_file.exists():
            with open(theme_file, encoding="utf-8") as f:
                theme_data = json.load(f)

        # Find or create author entry
        author_entry = None
        for v in theme_data.get("views", []):
            if v.get("author") == author:
                author_entry = v
                break

        new_view = {
            "date": note_date,
            "view": theme_entry.get("detailed_view", ""),
            "sentiment": "",
            "theme_name": theme_entry.get("theme", ""),
        }

        if author_entry is None:
            theme_data["views"].append({
                "author": author,
                "current": new_view,
                "history": [],
            })
        else:
            if author_entry.get("current"):
                author_entry["history"].insert(0, author_entry["current"])
                author_entry["history"] = author_entry["history"][:MAX_HISTORY]
            author_entry["current"] = new_view

        with open(theme_file, "w", encoding="utf-8") as f:
            json.dump(theme_data, f, indent=2, ensure_ascii=False)
            f.write("\n")


# ---------------------------------------------------------------------------
# Macro summary
# ---------------------------------------------------------------------------

def _rebuild_macro_summary():
    """Rebuild macro_summary.json and macro_summary.md from all author summaries."""
    import click

    authors = _list_all_authors()
    if not authors:
        return

    all_author_summaries = {}
    total_notes = 0

    for author in authors:
        author_dir = MACRO_DIR / "authors" / author
        summary = _load_author_summary(author_dir)
        all_author_summaries[author] = summary
        total_notes += summary.get("notes_count", 0)

    # Build macro_summary.json
    macro_summary = {
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "authors_count": len(authors),
        "total_notes": total_notes,
        "authors": {},
    }

    for author, summary in all_author_summaries.items():
        current_views = {}
        for topic in VIEW_TOPICS:
            if topic in summary.get("views", {}):
                current_views[topic] = summary["views"][topic].get("current")
        macro_summary["authors"][author] = {
            "notes_count": summary.get("notes_count", 0),
            "latest_note_date": summary.get("latest_note_date"),
            "current_views": current_views,
        }

    summary_json_path = MACRO_DIR / "macro_summary.json"
    with open(summary_json_path, "w", encoding="utf-8") as f:
        json.dump(macro_summary, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # Generate macro_summary.md
    md = _generate_macro_summary_md(macro_summary, all_author_summaries)
    summary_md_path = MACRO_DIR / "macro_summary.md"
    with open(summary_md_path, "w", encoding="utf-8") as f:
        f.write(md)

    click.echo(f"  Saved {summary_json_path.relative_to(PROJECT_ROOT)}")
    click.echo(f"  Saved {summary_md_path.relative_to(PROJECT_ROOT)}")


def _short_author(author):
    """Shorten author name for table headers: 'Michael Hartnett (BofA)' → 'Hartnett (BofA)'"""
    m = re.match(r'(?:\w+\s+)?(\w+)\s*(\(.*\))?', author)
    if m:
        surname = m.group(1)
        firm = m.group(2) or ""
        return f"{surname} {firm}".strip()
    return author[:20]


def _generate_macro_summary_md(macro_summary, all_author_summaries):
    updated = macro_summary["last_updated"][:10]
    n_authors = macro_summary["authors_count"]
    n_notes = macro_summary["total_notes"]

    lines = [
        f"# Macro Research Summary",
        f"Last updated: {updated} | Authors: {n_authors} | Notes: {n_notes}",
        "",
    ]

    authors = list(macro_summary["authors"].keys())
    short_names = [_short_author(a) for a in authors]

    # Cross-author comparison table
    lines.append("## Cross-Author Views Comparison")
    lines.append("")

    header = "| Topic |"
    divider = "|-------|"
    for sn in short_names:
        header += f" {sn} |"
        divider += "------|"
    lines.append(header)
    lines.append(divider)

    topic_labels = {
        "growth_outlook": "US Growth",
        "inflation_outlook": "Inflation",
        "monetary_policy": "Fed Path",
        "rates_and_yields": "10Y Yield",
        "fx_outlook": "USD View",
        "equity_outlook": "Equity View",
        "commodities": "Commodities",
        "geopolitics": "Key Risk",
        "flows_and_positioning": "Positioning",
    }

    for topic in VIEW_TOPICS:
        label = topic_labels.get(topic, topic)
        row = f"| {label} |"
        for author in authors:
            views = macro_summary["authors"][author].get("current_views", {})
            cv = views.get(topic)
            if cv:
                # Compact view: sentiment + short view
                sentiment = cv.get("sentiment", "")
                view = cv.get("view", "")
                # Truncate view for table
                short_view = view[:40] + "..." if len(view) > 40 else view
                row += f" {short_view} |"
            else:
                row += " N/A |"
        lines.append(row)

    lines.append("")

    # Latest notes table
    lines.append("## Latest Notes")
    lines.append("")
    lines.append("| Date | Author | Title |")
    lines.append("|------|--------|-------|")

    recent = []
    for author in authors:
        author_dir = MACRO_DIR / "authors" / author / "notes"
        for nf in sorted(author_dir.glob("*.json"), reverse=True)[:3]:
            try:
                with open(nf, encoding="utf-8") as f:
                    nd = json.load(f)
                meta = nd.get("metadata", {})
                date = meta.get("date", "")
                title = meta.get("title", "")
                recent.append((date, _short_author(author), title))
            except Exception:
                pass

    for date, author_short, title in sorted(recent, key=lambda x: x[0] or "", reverse=True)[:15]:
        try:
            dt = datetime.strptime(date, "%Y-%m-%d")
            date_fmt = dt.strftime("%b-%d")
        except ValueError:
            date_fmt = date
        lines.append(f"| {date_fmt} | {author_short} | {title} |")

    lines.append("")

    # --- Detailed per-author sections ---
    lines.append("---")
    lines.append("")
    lines.append("## Detailed Author Views")
    lines.append("")

    sentiment_icons = {
        "positive": "BULLISH", "negative": "BEARISH", "neutral": "NEUTRAL", "mixed": "MIXED",
        "hawkish": "HAWKISH", "dovish": "DOVISH", "uncertain": "UNCERTAIN",
    }

    for author in authors:
        summary = all_author_summaries.get(author, {})
        n = summary.get("notes_count", 0)
        if n == 0:
            continue
        latest = summary.get("latest_note_date", "?")
        views = summary.get("views", {})

        lines.append(f"### {author}")
        lines.append(f"*{n} notes | Latest: {latest}*")
        lines.append("")

        for topic in VIEW_TOPICS:
            if topic not in views:
                continue
            data = views[topic]
            current = data.get("current", {})
            if not current or not current.get("view"):
                continue

            label = topic_labels.get(topic, topic.replace("_", " ").title())
            date = current.get("date", "?")
            view = current.get("view", "N/A")
            sentiment = current.get("sentiment", "")
            sentiment_tag = sentiment_icons.get(sentiment.lower(), sentiment.upper()) if sentiment else ""

            lines.append(f"**{label}** ({date}) {f'[{sentiment_tag}]' if sentiment_tag else ''}")
            lines.append(f"> {view}")
            lines.append("")

            # Show view history if there are meaningful changes
            history = data.get("history", [])
            if history:
                prev = history[0]  # most recent previous view
                prev_view = prev.get("view", "")
                prev_date = prev.get("date", "?")
                prev_sentiment = prev.get("sentiment", "")
                prev_tag = sentiment_icons.get(prev_sentiment.lower(), prev_sentiment.upper()) if prev_sentiment else ""

                # Show the shift
                if prev_view and prev_view != view:
                    lines.append(f"*Previously ({prev_date}) [{prev_tag}]: {prev_view}*")
                    lines.append("")

                # If there's deeper history, show the trajectory
                if len(history) >= 2:
                    trajectory = []
                    for h in reversed(history[-3:]):
                        h_date = h.get("date", "?")
                        try:
                            h_dt = datetime.strptime(h_date, "%Y-%m-%d")
                            h_fmt = h_dt.strftime("%b-%d")
                        except ValueError:
                            h_fmt = h_date
                        h_sent = h.get("sentiment", "")
                        h_tag = sentiment_icons.get(h_sent.lower(), "") if h_sent else ""
                        h_view_short = h.get("view", "")[:50]
                        trajectory.append(f"{h_fmt}: {h_view_short} [{h_tag}]")
                    try:
                        c_dt = datetime.strptime(date, "%Y-%m-%d")
                        c_fmt = c_dt.strftime("%b-%d")
                    except ValueError:
                        c_fmt = date
                    trajectory.append(f"{c_fmt}: {view[:50]} [{sentiment_tag}] ← CURRENT")
                    arrow = " → "
                    lines.append(f"*Trajectory: {arrow.join(trajectory)}*")
                    lines.append("")

        # Active trades
        trades = summary.get("recommended_trades_history", [])
        if trades:
            latest_trades = trades[0]
            t_date = latest_trades.get("date", "?")
            trade_list = latest_trades.get("trades", [])
            if trade_list:
                lines.append(f"**Active Trades** ({t_date})")
                for t in trade_list:
                    trade = t.get("trade", "?")
                    rationale = t.get("rationale", "")
                    lines.append(f"- **{trade}**: {rationale}")
                lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# store-macro
# ---------------------------------------------------------------------------

def store_macro(file_path, author):
    """Store a macro research file under the given author."""
    import click

    author_dir = _ensure_dirs(author)
    notes_dir = author_dir / "notes"

    src = Path(file_path)
    if not src.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    # Copy file
    dest_name = f"{_today_prefix()}_{src.name}"
    dest = notes_dir / dest_name
    shutil.copy2(src, dest)
    click.echo(f"Copied to {dest.relative_to(PROJECT_ROOT)}")

    # Extract text
    click.echo(f"Extracting text from {src.name}...")
    text = _extract_file_text(src)
    click.echo(f"  Extracted {len(text):,} characters")

    # Send to Claude (retry on truncated/invalid JSON)
    click.echo("Analysing with Claude API...")
    client = Anthropic(max_retries=3, timeout=600.0)
    prompt = _build_extraction_prompt(author) + text
    note_data = None
    for json_attempt in range(3):
        raw = _call_api(client, [{"role": "user", "content": prompt}], max_tokens=16384)
        try:
            note_data = _parse_json_response(raw)
            break
        except json.JSONDecodeError as e:
            click.echo(f"  JSON parse failed (attempt {json_attempt + 1}/3): {e}; retrying API call...")
    if note_data is None:
        raise RuntimeError("Extraction call produced unparsable JSON after 3 attempts")
    json_path = notes_dir / f"{dest_name}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(note_data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    meta = note_data.get("metadata", {})
    click.echo(f"Saved: {json_path.relative_to(PROJECT_ROOT)}")
    click.echo(f"  Author: {meta.get('author', 'Unknown')}")
    click.echo(f"  Date:   {meta.get('date', 'Unknown')}")
    click.echo(f"  Title:  {meta.get('title', 'Unknown')}")

    # Second pass: View Evolution & Cross-Author Comparison
    click.echo("\nGenerating view evolution & comparison...")
    existing_author_summary = _load_author_summary(author_dir)
    macro_summary = None
    macro_path = MACRO_DIR / "macro_summary.json"
    if macro_path.exists():
        with open(macro_path, encoding="utf-8") as f:
            macro_summary = json.load(f)

    # Include trade history for macro-specific trade context
    trades_history = existing_author_summary.get("recommended_trades_history")

    second_prompt = build_second_pass_prompt(
        new_note_json=note_data,
        author_history_json=existing_author_summary.get("views"),
        summary_json=macro_summary,
        trades_history=trades_history,
    )
    view_evolution = _call_api(client, [{"role": "user", "content": second_prompt}], max_tokens=16384)
    report = merge_analysis_report(note_data, view_evolution)

    # Re-save JSON with complete analysis_report
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(note_data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # Print and save the analysis report
    click.echo("")
    try:
        click.echo(report)
    except UnicodeEncodeError:
        click.echo(report.encode("ascii", errors="replace").decode("ascii"))

    summary_md_path = notes_dir / f"{dest_name}_summary.md"
    with open(summary_md_path, "w", encoding="utf-8") as f:
        f.write(report)
    click.echo(f"\nSaved summary: {summary_md_path.relative_to(PROJECT_ROOT)}")

    # Rebuild author summary
    click.echo("\nRebuilding author summary...")
    _rebuild_author_summary(author_dir)

    # Update themes
    _update_themes(note_data, author)

    # Rebuild macro summary
    click.echo("Rebuilding macro summary...")
    _rebuild_macro_summary()
    click.echo("Done.")


# ---------------------------------------------------------------------------
# analyse-macro
# ---------------------------------------------------------------------------

ANALYSE_MACRO_PROMPT = """\
You are a macro research analyst at a global macro hedge fund running an equity long/short strategy. I'm providing:

1. NEW MACRO RESEARCH: A newly published macro research document
2. EXISTING MACRO SUMMARY: A summary of all stored macro research views across multiple authors
3. AUTHOR'S PREVIOUS VIEWS WITH HISTORY: The author's current and historical positions on each topic (if available)

Provide an extremely detailed analysis:

## A. DETAILED SUMMARY OF NEW RESEARCH
Comprehensive summary of the new document. Be extremely specific with all numbers, data points, forecasts, and trade recommendations.

## B. AUTHOR VIEW EVOLUTION
Using the author's view history, trace how their stance has shifted over time on each major topic. Present as a timeline table for each topic where the view has changed. Highlight the overall narrative arc.

## C. CROSS-AUTHOR COMPARISON TABLE
| Topic | New Note | {other_authors} | Consensus |
For all major macro topics: growth, inflation, Fed policy, rates, FX, equities, commodities, positioning.

## D. KEY ALIGNMENTS WITH OTHER AUTHORS
Where does the new note agree with the macro consensus? Be specific.

## E. KEY DIVERGENCES FROM OTHER AUTHORS
Where does it diverge? Quantify differences, explain reasoning, note if the author is moving toward or away from consensus.

## F. IMPLICATIONS FOR EQUITY PORTFOLIO
Given an equity long/short strategy:
- Sectors that benefit/suffer
- Single-name implications
- Portfolio positioning shifts
- Hedges recommended

Be extremely detailed and specific. Use actual numbers.
"""


def analyse_macro(file_path):
    """Analyse a new macro note against the existing research base."""
    import click

    src = Path(file_path)
    if not src.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    client = Anthropic(max_retries=3, timeout=600.0)

    # Extract text
    click.echo(f"Extracting text from {src.name}...")
    text = _extract_file_text(src)
    click.echo(f"  Extracted {len(text):,} characters")

    # First pass: extract structured data (use generic author)
    click.echo("Extracting structured data...")
    extraction_prompt = _build_extraction_prompt("Unknown") + text
    raw = _call_api(client, [{"role": "user", "content": extraction_prompt}], max_tokens=8192)
    new_note = _parse_json_response(raw)

    # Try to identify author
    meta = new_note.get("metadata", {})
    detected_author = meta.get("author", "")
    detected_firm = meta.get("firm", "")
    if detected_author and detected_firm:
        suggested = f"{detected_author} ({detected_firm})"
    elif detected_author:
        suggested = detected_author
    else:
        suggested = ""

    if suggested:
        click.echo(f"  Detected author: {suggested}")

    # Load macro summary
    macro_summary = None
    macro_path = MACRO_DIR / "macro_summary.json"
    if macro_path.exists():
        with open(macro_path, encoding="utf-8") as f:
            macro_summary = json.load(f)

    # Load author history if we have previous notes
    author_history = None
    if suggested:
        # Check for existing author folder (fuzzy match)
        for existing_author in _list_all_authors():
            if (suggested.lower() in existing_author.lower() or
                    existing_author.lower() in suggested.lower()):
                author_dir = MACRO_DIR / "authors" / existing_author
                author_history = _load_author_summary(author_dir)
                break

    # Build analysis prompt
    prompt = ANALYSE_MACRO_PROMPT
    prompt += f"\n\n--- NEW MACRO RESEARCH (structured extraction) ---\n{json.dumps(new_note, indent=2)}\n"

    if macro_summary:
        prompt += f"\n--- EXISTING MACRO SUMMARY ---\n{json.dumps(macro_summary, indent=2)}\n"
    else:
        prompt += "\n--- EXISTING MACRO SUMMARY ---\nNo existing macro research stored.\n"

    if author_history:
        prompt += f"\n--- AUTHOR VIEW HISTORY ---\n{json.dumps(author_history, indent=2)}\n"
    else:
        prompt += "\n--- AUTHOR VIEW HISTORY ---\nNo previous notes from this author.\n"

    prompt += f"\n--- RAW TEXT (for additional detail) ---\n{text[:15000]}\n"

    click.echo("Running comparative analysis...")
    analysis = _call_api(client, [{"role": "user", "content": prompt}], max_tokens=16384)

    click.echo("")
    click.echo(analysis)

    # Save analysis
    analyses_dir = MACRO_DIR / "analyses"
    analyses_dir.mkdir(parents=True, exist_ok=True)
    title_slug = re.sub(r'[^\w\s-]', '', meta.get("title", "unknown")[:30]).strip().replace(" ", "_")
    author_slug = re.sub(r'[^\w\s-]', '', suggested[:20]).strip().replace(" ", "_") if suggested else "unknown"
    analysis_filename = f"{_today_prefix()}_{author_slug}_{title_slug}_analysis.md"
    analysis_path = analyses_dir / analysis_filename
    with open(analysis_path, "w", encoding="utf-8") as f:
        f.write(analysis)
    click.echo(f"\nSaved analysis: {analysis_path.relative_to(PROJECT_ROOT)}")

    # Ask to store
    if click.confirm("\nWould you like to add this to the stored macro research base?"):
        author = click.prompt("Author name", default=suggested)
        store_macro(file_path, author)
    else:
        click.echo("Research not stored.")


# ---------------------------------------------------------------------------
# Show commands
# ---------------------------------------------------------------------------

def show_macro_summary():
    import click
    md_path = MACRO_DIR / "macro_summary.md"
    if not md_path.exists():
        click.echo("No macro research summary found.")
        click.echo("Run: python main.py store-macro --file <path> --author <name>")
        return
    click.echo(md_path.read_text(encoding="utf-8"))


def show_author_summary(author):
    import click
    author_dir = MACRO_DIR / "authors" / author
    summary_path = author_dir / "author_summary.json"
    if not summary_path.exists():
        click.echo(f"No summary found for '{author}'.")
        return

    with open(summary_path, encoding="utf-8") as f:
        summary = json.load(f)

    click.echo(f"# {author}")
    click.echo(f"{summary.get('notes_count', 0)} notes | Latest: {summary.get('latest_note_date', 'N/A')}")
    click.echo("")

    for topic in VIEW_TOPICS:
        if topic not in summary.get("views", {}):
            continue
        data = summary["views"][topic]
        current = data.get("current", {})
        label = topic.replace("_", " ").title()
        click.echo(f"## {label}")
        click.echo(f"  Current ({current.get('date', '?')}): {current.get('view', 'N/A')}")
        click.echo(f"  Sentiment: {current.get('sentiment', 'N/A')}")

        history = data.get("history", [])
        if history:
            click.echo(f"  Recent changes:")
            for h in history[:3]:
                click.echo(f"    {h.get('date', '?')}: {h.get('view', '')[:60]}")
        click.echo("")


def show_author_history(author, topic=None):
    import click
    author_dir = MACRO_DIR / "authors" / author
    summary_path = author_dir / "author_summary.json"
    if not summary_path.exists():
        click.echo(f"No summary found for '{author}'.")
        return

    with open(summary_path, encoding="utf-8") as f:
        summary = json.load(f)

    click.echo(f"# {author} — View Evolution")
    click.echo(f"{summary.get('notes_count', 0)} notes stored | Latest: {summary.get('latest_note_date', 'N/A')}")
    click.echo("")

    sentiment_icons = {
        "positive": "●", "negative": "○", "neutral": "►", "mixed": "◆",
        "hawkish": "▲", "dovish": "▼",
        "accelerating": "●", "decelerating": "○", "stable": "►",
        "rising": "▲", "falling": "▼", "sticky": "►",
        "strengthening": "▲", "weakening": "▼", "range-bound": "►",
    }

    topics = [topic] if topic else VIEW_TOPICS

    for t in topics:
        if t not in summary.get("views", {}):
            continue

        data = summary["views"][t]
        current = data.get("current", {})
        history = data.get("history", [])

        label = t.replace("_", " ").title()
        click.echo(f"## {label}")

        # Build timeline (oldest to newest)
        timeline = list(reversed(history)) + [current]

        parts = []
        for entry in timeline:
            date = entry.get("date", "?")
            try:
                dt = datetime.strptime(date, "%Y-%m-%d")
                date_short = dt.strftime("%b-%y")
            except ValueError:
                date_short = date

            view = entry.get("view", "")[:40]
            sentiment = entry.get("sentiment", "")
            icon = sentiment_icons.get(sentiment.lower(), "")
            parts.append(f"{date_short}: {view} {icon}")

        click.echo(" → ".join(parts))
        click.echo("")

    click.echo("(● = positive/bullish, ○ = negative/bearish, ► = neutral, ▲ = hawkish, ▼ = dovish)")


def list_macro_authors():
    import click
    authors = _list_all_authors()
    if not authors:
        click.echo("No macro research authors found.")
        click.echo("Run: python main.py store-macro --file <path> --author <name>")
        return

    for author in authors:
        author_dir = MACRO_DIR / "authors" / author
        summary = _load_author_summary(author_dir)
        n = summary.get("notes_count", 0)
        latest = summary.get("latest_note_date", "N/A")
        click.echo(f"\n{author}")
        click.echo(f"  Notes: {n} | Latest: {latest}")

        # Show current top-line views
        views = summary.get("views", {})
        for topic in ["growth_outlook", "monetary_policy", "equity_outlook"]:
            if topic in views:
                cv = views[topic].get("current", {})
                label = topic.replace("_", " ").title()
                view_short = (cv.get("view") or "")[:50]
                click.echo(f"  {label}: {view_short}")
