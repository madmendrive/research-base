"""Bot pipeline — orchestrates triage → store primary → cross-cutting analysis.

Single entry point: ingest_and_analyse(pdf_path) -> dict
Designed to be called from bot.py off the event loop.
"""

import contextlib
import json
import shutil
from datetime import datetime
from pathlib import Path

from anthropic import Anthropic

from scripts.triage import triage_document, format_triage_for_user

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
PENDING_REVIEW_DIR = DATA_DIR / "_pending_review"
ROUTING_LOG_PATH = DATA_DIR / "_routing_log.jsonl"


# ---------------------------------------------------------------------------
# Routing log + low-confidence holding folder
# ---------------------------------------------------------------------------

def _append_routing_log(record: dict) -> None:
    """Append-only audit log of every classification decision.
    One JSON object per line so it's easy to grep / tail / import to pandas.
    """
    ROUTING_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(ROUTING_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _hold_for_review(triage: dict, pdf_path: Path) -> str:
    """Copy a low-confidence-triaged file to data/_pending_review/ instead of
    auto-storing. Returns the user-facing message for the bot/sweeper to send.
    """
    PENDING_REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    dest = PENDING_REVIEW_DIR / f"{today}_{pdf_path.name}"
    if not dest.exists():
        shutil.copy2(pdf_path, dest)
    pt = triage["primary_type"]
    cat = triage.get("category") or ""
    ps = triage["primary_subject"]
    rationale = triage.get("rationale", "")
    return (
        f"⚠ LOW CONFIDENCE — held for review.\n"
        f"Triage best guess: {pt}{('/' + cat) if cat else ''} → {ps}\n"
        f"Rationale: {rationale}\n"
        f"File: data/_pending_review/{dest.name}\n"
        f"To accept: python main.py store-{pt.replace('_', '-')} ... <path>\n"
        f"Or use the Telegram override commands: /research TICKER, /macro AUTHOR, /thematic THEME"
    )


# ---------------------------------------------------------------------------
# click monkey-patch: skip interactive prompts inside store_*/analyse_* calls
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _suppress_click_prompts():
    """Auto-decline any click.confirm() and return empty string for click.prompt()."""
    import click
    orig_confirm = click.confirm
    orig_prompt = click.prompt
    click.confirm = lambda *a, **kw: False
    click.prompt = lambda *a, **kw: ""
    try:
        yield
    finally:
        click.confirm = orig_confirm
        click.prompt = orig_prompt


def _snapshot(dir_path: Path, pattern: str = "*.md") -> set[Path]:
    if not dir_path.exists():
        return set()
    return set(dir_path.glob(pattern))


def _newest_diff(dir_path: Path, before: set[Path], pattern: str = "*.md") -> Path | None:
    after = _snapshot(dir_path, pattern)
    new = after - before
    if not new:
        return None
    return max(new, key=lambda p: p.stat().st_mtime)


# ---------------------------------------------------------------------------
# Primary storage — wraps existing store_* CLI functions
# ---------------------------------------------------------------------------

def _store_primary_research(ticker: str, pdf_path: Path) -> str:
    """Call store_research, return the per-document summary markdown."""
    from scripts.research import store_research
    notes_dir = DATA_DIR / ticker / "research" / "notes"
    before = _snapshot(notes_dir, "*_summary.md")
    with _suppress_click_prompts():
        store_research(ticker, str(pdf_path))
    new = _newest_diff(notes_dir, before, "*_summary.md")
    return new.read_text(encoding="utf-8") if new else "(stored; no per-document summary file produced)"


def _store_primary_macro(author: str, pdf_path: Path, category: str = "Macro") -> str:
    """Store an author-driven research doc under data/{category}/.

    category="Macro" → uses scripts.macro.store_macro (data/Macro/)
    category="Semis" → uses scripts.semis.store_semis (data/Semis/)
    """
    if category == "Semis":
        from scripts.semis import store_semis as _store
        summary_md = DATA_DIR / "Semis" / "macro_summary.md"
    else:
        from scripts.macro import store_macro as _store
        summary_md = DATA_DIR / "Macro" / "macro_summary.md"
    with _suppress_click_prompts():
        _store(str(pdf_path), author)
    return summary_md.read_text(encoding="utf-8") if summary_md.exists() else f"(stored under data/{category}/; no rolling summary file)"


def _store_primary_thematic(theme: str, pdf_path: Path) -> str:
    from scripts.thematic import store_thematic
    notes_dir = DATA_DIR / "Thematic" / theme / "notes"
    before = _snapshot(notes_dir, "*_summary.md")
    with _suppress_click_prompts():
        store_thematic(theme, [str(pdf_path)])
    new = _newest_diff(notes_dir, before, "*_summary.md")
    return new.read_text(encoding="utf-8") if new else "(stored; no per-document summary file produced)"


# ---------------------------------------------------------------------------
# Cross-cutting analyses — reuse existing analyse_* (auto-declining storage)
# ---------------------------------------------------------------------------

def _cross_analyse_ticker(ticker: str, pdf_path: Path) -> str:
    """Run analyse_research without storing; return the saved analysis markdown."""
    from scripts.research import analyse_research
    analyses_dir = DATA_DIR / ticker / "research" / "analyses"
    before = _snapshot(analyses_dir, "*_analysis.md")
    with _suppress_click_prompts():
        analyse_research(ticker, file_path=str(pdf_path))
    new = _newest_diff(analyses_dir, before, "*_analysis.md")
    return new.read_text(encoding="utf-8") if new else "(analysis ran but no file was produced)"


def _cross_analyse_theme(theme: str, pdf_path: Path) -> str:
    from scripts.thematic import analyse_thematic
    analyses_dir = DATA_DIR / "Thematic" / theme / "analyses"
    before = _snapshot(analyses_dir, "*_analysis.md")
    with _suppress_click_prompts():
        analyse_thematic(theme, str(pdf_path))
    new = _newest_diff(analyses_dir, before, "*_analysis.md")
    return new.read_text(encoding="utf-8") if new else "(analysis ran but no file was produced)"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _derive_secondaries(triage: dict) -> list[tuple[str, str, str]]:
    """Return list of (label, kind, target) for cross-cutting analysis.

    Filters by materiality from the triage step — only 'significant' or
    'primary' mentions get a cross-cut. 'passing' mentions are skipped to
    avoid producing low-signal analyses that bloat cost and the knowledge
    base. (A 20-page SemiAnalysis piece can mention 15 tickers; only ~3-5
    are usually substantive.)

    If materiality is missing for an entity, default to 'significant' (i.e.
    include it). That's conservative — better a wasted cross-cut than a
    missing one.
    """
    primary_type = triage["primary_type"]
    primary_subject = triage["primary_subject"]
    ticker_mat = triage.get("materiality", {}).get("tickers", {}) or {}
    theme_mat = triage.get("materiality", {}).get("themes", {}) or {}

    secondaries = []
    for ticker in triage.get("tickers_covered", []):
        if primary_type == "single_name" and ticker == primary_subject:
            continue
        if ticker_mat.get(ticker, "significant") == "passing":
            continue
        secondaries.append((f"Cross-read: {ticker}", "ticker", ticker))
    for theme in triage.get("themes_touched", []):
        if primary_type == "thematic" and theme == primary_subject:
            continue
        if theme_mat.get(theme, "significant") == "passing":
            continue
        secondaries.append((f"Cross-read: {theme} (theme)", "theme", theme))
    return secondaries


def _store_primary(triage: dict, pdf_path: Path) -> str:
    pt = triage["primary_type"]
    ps = triage["primary_subject"]
    confidence = triage.get("confidence", "low")

    # Routing log: append-only audit of EVERY decision (held or stored).
    _append_routing_log({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "file": pdf_path.name,
        "primary_type": pt,
        "category": triage.get("category"),
        "primary_subject": ps,
        "confidence": confidence,
        "routed_to": "_pending_review" if confidence == "low" else (
            f"data/{ps}/research" if pt == "single_name"
            else f"data/{triage.get('category', 'Macro')}/authors/{ps}" if pt in ("macro", "news_article")
            else f"data/Thematic/{ps}" if pt == "thematic"
            else "?"
        ),
        "tickers_covered": triage.get("tickers_covered", []),
        "themes_touched": triage.get("themes_touched", []),
    })

    # Confidence gate — low confidence goes to the holding folder, NOT silent storage.
    if confidence == "low":
        return _hold_for_review(triage, pdf_path)

    if pt == "single_name":
        return _store_primary_research(ps, pdf_path)
    if pt == "thematic":
        return _store_primary_thematic(ps, pdf_path)
    if pt in ("macro", "news_article"):
        author = ps if pt == "macro" else "News Article"
        category = triage.get("category") or "Macro"
        return _store_primary_macro(author, pdf_path, category=category)
    return f"(unknown primary_type {pt!r}; nothing stored)"


def ingest_and_analyse(pdf_path: str | Path) -> dict:
    """Full drop-file-and-go pipeline.

    Returns dict with:
      triage: triage result dict
      triage_summary: human-readable triage line(s)
      primary_report: markdown of primary store summary
      secondaries: list of {label, kind, target, report or error}
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)

    client = Anthropic(max_retries=3, timeout=180.0)

    triage = triage_document(pdf_path, client=client)

    primary_report = _store_primary(triage, pdf_path)

    secondaries_out = []
    for label, kind, target in _derive_secondaries(triage):
        try:
            if kind == "ticker":
                rpt = _cross_analyse_ticker(target, pdf_path)
            else:
                rpt = _cross_analyse_theme(target, pdf_path)
            secondaries_out.append({"label": label, "kind": kind, "target": target, "report": rpt})
        except Exception as e:
            secondaries_out.append({"label": label, "kind": kind, "target": target, "error": str(e)})

    return {
        "triage": triage,
        "triage_summary": format_triage_for_user(triage),
        "primary_report": primary_report,
        "secondaries": secondaries_out,
    }
