"""Bulk cross-cut — second pass over the bulk-loaded corpus.

For every doc bulk-ingest stored, run analyse_research / analyse_thematic
against each OTHER ticker/theme it touches (excluding its primary entity).
Skips pairs already done. Resumable. Cost-aware (warns before running).

CLI: python main.py bulk-cross-cut [--dry-run] [--limit N] [--yes] [--folder PATH]
"""

import json
import os
import time
import traceback
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
BULK_STATE_PATH = DATA_DIR / "_bulk_ingest_state.json"
CC_STATE_PATH = DATA_DIR / "_bulk_cross_cut_state.json"

# Rough $0.30/call assumed for cost estimates (Sonnet 4, ~15k in + 3-5k out).
EST_COST_PER_PAIR_USD = 0.30
# Rough seconds per cross-cut analysis.
EST_SECONDS_PER_PAIR = 40


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _load_state(path: Path) -> dict:
    if not path.exists():
        return {"processed_pairs": {}, "failed_pairs": {}, "runs": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_state(state: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def _pair_key(file_hash: str, kind: str, target: str) -> str:
    return f"{file_hash}:{kind}:{target}"


FAST_STATE_PATH = DATA_DIR / "_fast_ingest_state.json"
FAST_STAGING_DIR = DATA_DIR / "_staging_ingest"


def _fast_ingest_records() -> dict:
    """Corpus records for fast-ingested docs, in the classic 'processed' shape.

    The classic bulk state only knows sweeper/bulk-ingest docs; the corpus was
    largely built with bulk-ingest-fast, whose commits live in
    _fast_ingest_state.json with triage in _staging_ingest/{digest}.json.
    Without this the cross-cut pass sees an empty corpus. Secondary lists are
    materiality-gated here (mirroring bot_pipeline._derive_secondaries);
    passing mentions never become pairs. The stored copy is used as the
    source PDF — inbox originals may have moved since ingestion."""
    from scripts.tickers import canonicalize_ticker
    from scripts.triage import _existing_themes, _load_companies

    if not FAST_STATE_PATH.exists():
        return {}
    try:
        state = json.loads(FAST_STATE_PATH.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    # The fast path's triage doesn't always obey the strict theme constraint,
    # so themes_touched can contain invented themes and tickers_covered
    # companies outside the coverage universe. Cross-cutting those would
    # create junk entity dirs — gate targets to known tickers/themes.
    # canonicalize_ticker normalizes but returns unknowns unchanged, so
    # coverage is a companies.json membership check.
    known_themes = set(_existing_themes())
    covered = set(_load_companies())
    records = {}
    for digest, rec in (state.get("committed") or {}).items():
        if rec.get("status") == "pending_review":
            continue
        stage_path = FAST_STAGING_DIR / f"{digest}.json"
        if not stage_path.exists():
            continue
        try:
            staged = json.loads(stage_path.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        triage = staged.get("triage") or {}
        materiality = triage.get("materiality") or {}
        ticker_mat = materiality.get("tickers") or {}
        theme_mat = materiality.get("themes") or {}
        records[digest] = {
            "file": rec.get("file"),
            "source_path": rec.get("stored_path") or staged.get("source_path", ""),
            "primary_type": triage.get("primary_type", ""),
            "primary_subject": triage.get("primary_subject", ""),
            # dict.fromkeys: triage theme lists are not deduped upstream —
            # a repeated theme would yield the same pair twice.
            "tickers_covered": list(dict.fromkeys(
                t for t in triage.get("tickers_covered") or []
                if ticker_mat.get(t, "significant") != "passing"
                and (canonicalize_ticker(t) or t) in covered
            )),
            "themes_touched": list(dict.fromkeys(
                t for t in triage.get("themes_touched") or []
                if theme_mat.get(t, "significant") != "passing"
                and t in known_themes
            )),
        }
    return records


# ---------------------------------------------------------------------------
# Pair discovery
# ---------------------------------------------------------------------------

def _derive_pairs(bulk_state: dict, cc_state: dict, fallback_folder: Path | None) -> list[dict]:
    """Build the list of (file, kind, target) cross-cut jobs.

    Skips:
      - pairs already done (in cc_state.processed_pairs)
      - docs whose source PDF no longer exists (also looks in fallback_folder by name)
      - primary entity (e.g. don't cross-cut KLAC against itself)
    """
    pairs = []
    processed = cc_state.get("processed_pairs", {})

    for file_hash, rec in bulk_state.get("processed", {}).items():
        if rec.get("skipped_reason"):
            continue

        # Resolve source PDF — try the recorded path first, then fallback by filename
        src = Path(rec.get("source_path", ""))
        if not src.exists() and fallback_folder is not None:
            candidate = fallback_folder / rec.get("file", "")
            if candidate.exists():
                src = candidate

        primary_type = rec.get("primary_type", "")
        primary_subject = rec.get("primary_subject", "")

        for ticker in rec.get("tickers_covered", []):
            if primary_type == "single_name" and ticker == primary_subject:
                continue
            key = _pair_key(file_hash, "ticker", ticker)
            if key in processed:
                continue
            pairs.append({
                "file_hash": file_hash,
                "file": rec.get("file"),
                "source_path": str(src),
                "kind": "ticker",
                "target": ticker,
                "source_exists": src.exists(),
            })

        for theme in rec.get("themes_touched", []):
            if primary_type == "thematic" and theme == primary_subject:
                continue
            key = _pair_key(file_hash, "theme", theme)
            if key in processed:
                continue
            pairs.append({
                "file_hash": file_hash,
                "file": rec.get("file"),
                "source_path": str(src),
                "kind": "theme",
                "target": theme,
                "source_exists": src.exists(),
            })
    return pairs


def merged_corpus_state() -> dict:
    """Corpus = classic bulk-ingest docs + fast-ingested docs. Classic records
    win on digest collision (they carry the original source_path). Note: docs
    cross-cut by the real-time cross_cut jobs (Phase 2 of pipeline
    unification) are tracked in the jobs table, not here — re-running their
    pairs is harmless (analyse_* just writes another analysis file) but costs
    a call; review the plan when in doubt."""
    bulk_state = _load_state(BULK_STATE_PATH) if BULK_STATE_PATH.exists() else {}
    fast_records = _fast_ingest_records()
    if fast_records:
        bulk_state = dict(bulk_state)
        bulk_state["processed"] = {**fast_records, **bulk_state.get("processed", {})}
    return bulk_state


def derive_all_pairs(fallback_folder: Path | None = None):
    """(pending pairs, cc_state, corpus size) over the merged corpus —
    shared by the realtime runner and the batch backfill."""
    bulk_state = merged_corpus_state()
    cc_state = _load_state(CC_STATE_PATH)
    pairs = _derive_pairs(bulk_state, cc_state, fallback_folder)
    return pairs, cc_state, len(bulk_state.get("processed", {}))


# ---------------------------------------------------------------------------
# Telegram notify (reuses bulk-ingest helper)
# ---------------------------------------------------------------------------

def _telegram_notify(text: str) -> None:
    from scripts.bulk_ingest import _telegram_notify as _notify
    _notify(text)


# ---------------------------------------------------------------------------
# Main entry — called from main.py
# ---------------------------------------------------------------------------

def bulk_cross_cut(folder: str = "", dry_run: bool = False, limit: int = 0,
                   yes: bool = False, notify: bool = True) -> None:
    """Run cross-cutting analysis for every (doc × other entity) pair.

    folder: optional fallback folder to resolve source PDFs by filename if the
            originally-recorded source_path doesn't exist anymore.
    dry_run: list what would be cross-cut and show cost estimate; no API calls.
    limit: process at most this many pairs (0 = unlimited).
    yes: skip the cost-confirmation prompt.
    notify: send Telegram summary at end.
    """
    import click
    from scripts.bot_pipeline import _cross_analyse_ticker, _cross_analyse_theme

    fallback = Path(folder).expanduser().resolve() if folder else None
    pairs, cc_state, total_in_corpus = derive_all_pairs(fallback)
    if total_in_corpus == 0:
        click.echo("No ingested documents found in _bulk_ingest_state.json or "
                   "_fast_ingest_state.json. Run an ingest first.")
        raise SystemExit(1)

    if not pairs:
        click.echo(f"Nothing to cross-cut. Corpus size: {total_in_corpus} doc(s). "
                   f"All pairs already processed: {len(cc_state.get('processed_pairs', {}))}.")
        return

    # Filter out pairs whose source PDF can't be found
    missing = [p for p in pairs if not p["source_exists"]]
    pairs = [p for p in pairs if p["source_exists"]]

    if limit and limit > 0:
        pairs = pairs[:limit]

    by_kind = {"ticker": 0, "theme": 0}
    for p in pairs:
        by_kind[p["kind"]] += 1

    est_cost = len(pairs) * EST_COST_PER_PAIR_USD
    est_minutes = len(pairs) * EST_SECONDS_PER_PAIR / 60

    click.echo("=== Bulk cross-cut plan ===")
    click.echo(f"Corpus: {total_in_corpus} doc(s) in bulk_ingest state")
    click.echo(f"Already done: {len(cc_state.get('processed_pairs', {}))} pair(s)")
    click.echo(f"To process now: {len(pairs)} pair(s)  ({by_kind['ticker']} ticker, {by_kind['theme']} theme)")
    if missing:
        click.echo(f"Skipped (source PDF missing): {len(missing)} pair(s)")
    click.echo(f"Estimated cost: ${est_cost:.0f}  |  Estimated time: {est_minutes:.0f} min")
    click.echo("")

    if dry_run:
        click.echo("DRY RUN — first 30 pairs that would run:")
        for p in pairs[:30]:
            click.echo(f"  {p['file'][:80]} -> {p['kind']}/{p['target']}")
        if len(pairs) > 30:
            click.echo(f"  ... and {len(pairs) - 30} more")
        return

    if not yes:
        if not click.confirm(f"\nProceed and spend ~${est_cost:.0f}?", default=False):
            click.echo("Aborted.")
            return

    run_start = time.time()
    succeeded = 0
    failed_now = 0

    for idx, p in enumerate(pairs, start=1):
        click.echo(f"[{idx}/{len(pairs)}] {p['file'][:60]} -> {p['kind']}/{p['target']}")
        t0 = time.time()
        try:
            if p["kind"] == "ticker":
                _cross_analyse_ticker(p["target"], Path(p["source_path"]))
            else:
                _cross_analyse_theme(p["target"], Path(p["source_path"]))
            elapsed = time.time() - t0
            cc_state.setdefault("processed_pairs", {})[_pair_key(p["file_hash"], p["kind"], p["target"])] = {
                "file": p["file"],
                "kind": p["kind"],
                "target": p["target"],
                "done_at": datetime.now().isoformat(timespec="seconds"),
                "elapsed_seconds": round(elapsed, 1),
            }
            _save_state(cc_state, CC_STATE_PATH)
            succeeded += 1
            click.echo(f"    ok ({elapsed:.0f}s)")
        except KeyboardInterrupt:
            click.echo("\nInterrupted. State saved; re-run to resume.")
            raise
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            tb = traceback.format_exc()
            click.echo(f"    FAILED: {err}")
            cc_state.setdefault("failed_pairs", {})[_pair_key(p["file_hash"], p["kind"], p["target"])] = {
                "file": p["file"],
                "kind": p["kind"],
                "target": p["target"],
                "error": err,
                "trace": tb,
                "failed_at": datetime.now().isoformat(timespec="seconds"),
            }
            _save_state(cc_state, CC_STATE_PATH)
            failed_now += 1

    elapsed_total = time.time() - run_start
    cc_state.setdefault("runs", []).append({
        "started_at": datetime.fromtimestamp(run_start).isoformat(timespec="seconds"),
        "succeeded": succeeded,
        "failed": failed_now,
        "elapsed_seconds": round(elapsed_total, 1),
    })
    _save_state(cc_state, CC_STATE_PATH)

    summary = (
        f"=== Bulk cross-cut summary ===\n"
        f"Pairs succeeded: {succeeded}  |  Failed: {failed_now}  |  "
        f"Elapsed: {elapsed_total/60:.1f} min"
    )
    click.echo("\n" + summary)
    if notify:
        _telegram_notify(summary)
