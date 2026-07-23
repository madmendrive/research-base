"""View-evolution second pass for fast-path-stored notes.

The classic store_* functions run this inline after extraction; the fast
pipeline stores the extraction immediately and enqueues a view_evolution job
that lands here. One Opus call per note — every other step is local
aggregation over stored note JSONs, mirroring store_research / store_macro /
store_thematic exactly.

Split into prepare (build the Opus prompt) and apply (merge the result +
refresh summaries) so the Batches API backfill can reuse both halves around
a batched call instead of a live one.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

VIEW_EVOLUTION_MARKER = "View Evolution & Cross-Author Comparison"


def _load_json(path: Path) -> Any:
    # errors="replace": a few pre-encoding-fix notes carry stray cp1252 bytes.
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def _save_note(path: Path, note_data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(note_data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def second_pass_done(note_data: dict) -> bool:
    """Done = stamped by a previous run, or the report is substantive with no
    placeholder left (a classic-stored note — its view evolution was merged in
    place of PENDING_SECOND_PASS, so there is no marker heading to look for).
    Running the pass on such a note would append a duplicate section."""
    report = note_data.get("analysis_report") or ""
    return bool(
        (note_data.get("metadata") or {}).get("second_pass_done")
        or (report.strip() and "PENDING_SECOND_PASS" not in report)
    )


def _second_pass_context(primary_type: str, category: str, note_data: dict,
                         entity_dir: Path):
    """Return (author_history, summary_json, trades_history) mirroring the
    classic store paths. entity_dir is the dir above notes/ — the ticker's
    research/, the theme dir, or the author dir — so no name canonicalization
    is needed here."""
    if primary_type == "single_name":
        from scripts.research import _source_label

        summary_path = entity_dir / "summary.json"
        existing = _load_json(summary_path) if summary_path.exists() else None
        author_history = None
        if existing:
            label = _source_label(note_data.get("metadata", {}))
            author_history = {
                "source": label,
                "consensus_estimates": existing.get("consensus_estimates", {}),
                "ratings": [r for r in existing.get("ratings", []) if r.get("source") == label],
            }
        return author_history, existing, None

    if primary_type == "thematic":
        from scripts.thematic import _source_label

        summary_path = entity_dir / "theme_summary.json"
        existing = _load_json(summary_path) if summary_path.exists() else None
        source_history = None
        if existing:
            label = _source_label(note_data.get("metadata", {}))
            for entry in existing.get("sources", []):
                if entry.get("source") == label:
                    source_history = entry
                    break
        return source_history, existing, None

    # macro / semis author note
    from scripts import macro

    with macro.use_category(category):
        author_summary = macro._load_author_summary(entity_dir)
        macro_path = macro._macro_dir() / "macro_summary.json"
        macro_summary = _load_json(macro_path) if macro_path.exists() else None
    return (
        author_summary.get("views"),
        macro_summary,
        author_summary.get("recommended_trades_history"),
    )


def prepare_second_pass(json_path: str | Path, *, primary_type: str,
                        subject: str, category: str = "Macro") -> dict | None:
    """Build the Opus view-evolution prompt for a stored note.
    Returns None when the note already has its second pass."""
    from scripts.analysis_report import build_second_pass_prompt

    json_path = Path(json_path)
    if not json_path.exists():
        raise FileNotFoundError(json_path)
    note_data = _load_json(json_path)
    if second_pass_done(note_data):
        return None

    entity_dir = json_path.parent.parent  # notes/ -> ticker research/ | theme dir | author dir
    author_history, summary_json, trades_history = _second_pass_context(
        primary_type, category, note_data, entity_dir)
    prompt = build_second_pass_prompt(
        new_note_json=note_data,
        author_history_json=author_history,
        summary_json=summary_json,
        trades_history=trades_history,
    )
    return {"prompt": prompt, "entity_dir": entity_dir}


def rebuild_entity_summaries(primary_type: str, subject: str, category: str,
                             entity_dir: Path) -> None:
    """Rebuild the entity-level rolling summaries (local aggregation, no API)."""
    if primary_type == "single_name":
        from scripts.research import rebuild_summary

        rebuild_summary(subject)
    elif primary_type == "thematic":
        from scripts.thematic import rebuild_theme_summary

        rebuild_theme_summary(subject)
    else:
        from scripts import macro

        with macro.use_category(category):
            macro._rebuild_author_summary(entity_dir)
            macro._rebuild_macro_summary()


def apply_second_pass(json_path: str | Path, *, primary_type: str, subject: str,
                      category: str = "Macro", view_evolution: str,
                      rebuild: bool = True) -> dict:
    """Merge an Opus view-evolution result into the stored note: replace the
    placeholder in analysis_report, overwrite the extraction-stub summary md,
    update per-note theme files (macro), and optionally rebuild the entity
    summaries. rebuild=False lets a bulk apply dedupe rebuilds per entity."""
    from scripts.analysis_report import merge_analysis_report

    json_path = Path(json_path)
    note_data = _load_json(json_path)
    if second_pass_done(note_data):
        return {"status": "already_done", "json_path": str(json_path)}

    full_report = merge_analysis_report(note_data, view_evolution)
    note_data.setdefault("metadata", {})["second_pass_done"] = (
        datetime.now(timezone.utc).isoformat(timespec="seconds"))
    _save_note(json_path, note_data)

    # Replace the fast path's extraction-only stub with the full report.
    md_path = json_path.with_name(json_path.name.removesuffix(".json") + "_summary.md")
    md_path.write_text(full_report, encoding="utf-8")

    entity_dir = json_path.parent.parent
    if primary_type not in {"single_name", "thematic"}:
        # Theme files aggregate per note (classic store_macro does this per
        # store), unlike the entity summaries which rebuild from all notes.
        from scripts import macro

        with macro.use_category(category):
            macro._update_themes(note_data, entity_dir.name)
    if rebuild:
        rebuild_entity_summaries(primary_type, subject, category, entity_dir)
    return {
        "status": "ok",
        "json_path": str(json_path),
        "summary_md": str(md_path),
        "view_evolution_chars": len(view_evolution),
    }


def run_second_pass(json_path: str | Path, *, primary_type: str, subject: str,
                    category: str = "Macro") -> dict:
    """Run the Opus view-evolution pass on an already-stored note JSON and
    refresh the entity's rolling summaries. Idempotent."""
    from scripts import research

    prep = prepare_second_pass(
        json_path, primary_type=primary_type, subject=subject, category=category)
    if prep is None:
        return {"status": "already_done", "json_path": str(json_path)}

    client = research.Anthropic(max_retries=3, timeout=600.0)
    view_evolution = research._call_api(
        client, [{"role": "user", "content": prep["prompt"]}],
        max_tokens=16384, model=research.SYNTHESIS_MODEL, offload="view_evolution")
    return apply_second_pass(
        json_path, primary_type=primary_type, subject=subject,
        category=category, view_evolution=view_evolution, rebuild=True)
