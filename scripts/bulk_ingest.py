"""Bulk ingest — one-shot backfill of PDFs into the knowledge base.

For each PDF in a folder: triage → store under primary type. No cross-cutting
(that's a separate pass once the base is populated). Resumable via a state file.

CLI: python main.py bulk-ingest --folder PATH [--dry-run] [--limit N] [--with-cross-cut]
"""

import hashlib
import json
import os
import time
import traceback
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
STATE_PATH = DATA_DIR / "_bulk_ingest_state.json"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {"processed": {}, "failed": {}, "runs": []}
    with open(STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save_state(state: dict) -> None:
    from scripts.fileio import write_json_atomic

    write_json_atomic(STATE_PATH, state, trailing_newline=False)


def _hash_file(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()[:16]


def _scan_pdfs(folder: Path) -> list[Path]:
    return sorted(
        p for p in folder.rglob("*.pdf")
        if p.is_file() and not p.name.startswith(".")
    )


# ---------------------------------------------------------------------------
# Auto-add helpers (real-run only)
# ---------------------------------------------------------------------------

COMPANIES_PATH = PROJECT_ROOT / "config" / "companies.json"


def _auto_add_tickers(proposals: list[dict]) -> list[str]:
    """Append any new tickers to companies.json. Returns list of tickers actually added."""
    if not proposals:
        return []
    with open(COMPANIES_PATH, encoding="utf-8") as f:
        companies = json.load(f)
    added = []
    for p in proposals:
        ticker = (p.get("ticker") or "").strip()
        name = (p.get("name") or "").strip()
        market = (p.get("market") or "").strip().upper()
        if not ticker or not name:
            continue
        if ticker in companies:
            continue
        if market not in ("US", "JP", "TW", "KR", "HK", "EU", "OTHER"):
            market = "US"  # safest default for unidentified market
        companies[ticker] = {
            "name": name,
            "market": market,
        }
        added.append(ticker)
    if added:
        with open(COMPANIES_PATH, "w", encoding="utf-8") as f:
            json.dump(companies, f, indent=2, ensure_ascii=False)
            f.write("\n")
    return added


def _auto_add_macro_authors(proposals: list[dict], category: str = "Macro") -> list[str]:
    """Create author folder under data/{category}/authors/. Returns list added."""
    if not proposals:
        return []
    base = DATA_DIR / category / "authors"
    added = []
    for p in proposals:
        name = (p.get("name") or "").strip()
        if not name or name in {"Unknown", "News Article"}:
            continue
        author_dir = base / name
        if author_dir.exists():
            continue
        (author_dir / "notes").mkdir(parents=True, exist_ok=True)
        added.append(name)
    return added


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------

def _process_one(pdf_path: Path, dry_run: bool, with_cross_cut: bool, client=None) -> dict:
    """Triage + store one PDF. Returns a summary record for logging.

    client: optional shared Anthropic client — passed to triage_document so
            prompt-cache hits persist across files in the same loop.
    """
    from scripts.triage import triage_document
    from scripts.bot_pipeline import (
        _store_primary,
        _derive_secondaries,
        _cross_analyse_ticker,
        _cross_analyse_theme,
    )

    t0 = time.time()
    triage = triage_document(pdf_path, client=client)

    record = {
        "file": pdf_path.name,
        "size_mb": round(pdf_path.stat().st_size / (1024 * 1024), 2),
        "primary_type": triage["primary_type"],
        "primary_subject": triage["primary_subject"],
        "tickers_covered": triage["tickers_covered"],
        "themes_touched": triage["themes_touched"],
        "category": triage.get("category"),
        "proposed_new_tickers": triage.get("proposed_new_tickers", []),
        "proposed_new_macro_authors": triage.get("proposed_new_macro_authors", []),
        "proposed_new_semis_authors": triage.get("proposed_new_semis_authors", []),
        "proposed_new_themes": triage.get("proposed_new_themes", []),
        "confidence": triage["confidence"],
        "held_for_review": triage["confidence"] == "low",
        "triage_seconds": round(time.time() - t0, 1),
    }

    if dry_run:
        record["status"] = "dry_run"
        return record

    # Real run: auto-add new tickers and authors BEFORE storing
    _auto_add_tickers(triage.get("proposed_new_tickers", []))
    _auto_add_macro_authors(triage.get("proposed_new_macro_authors", []), category="Macro")
    _auto_add_macro_authors(triage.get("proposed_new_semis_authors", []), category="Semis")

    t1 = time.time()
    _store_primary(triage, pdf_path)
    record["store_seconds"] = round(time.time() - t1, 1)

    if with_cross_cut:
        cross_results = []
        for label, kind, target in _derive_secondaries(triage):
            try:
                if kind == "ticker":
                    _cross_analyse_ticker(target, pdf_path)
                else:
                    _cross_analyse_theme(target, pdf_path)
                cross_results.append({"target": target, "kind": kind, "ok": True})
            except Exception as e:
                cross_results.append({"target": target, "kind": kind, "ok": False, "error": str(e)})
        record["cross_cuts"] = cross_results

    record["status"] = "stored"
    record["total_seconds"] = round(time.time() - t0, 1)
    return record


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _format_summary(records: list[dict], failed: list[dict], elapsed: float,
                    dry_run: bool = False) -> str:
    lines = []
    lines.append("=== Bulk ingest summary ===")
    lines.append(f"Files processed: {len(records)}  |  Failed: {len(failed)}  |  Elapsed: {elapsed/60:.1f} min")
    if dry_run:
        lines.append("(DRY RUN — nothing was stored or added)")

    by_type: dict[str, list[str]] = {}
    for r in records:
        label = r["primary_type"]
        if r.get("category"):
            label = f"{label} / {r['category']}"
        by_type.setdefault(label, []).append(f"{r['primary_subject']} ({r['file']})")
    for ptype, entries in sorted(by_type.items()):
        lines.append(f"\n{ptype} — {len(entries)} doc(s):")
        for e in entries[:50]:
            lines.append(f"  - {e}")
        if len(entries) > 50:
            lines.append(f"  ... and {len(entries) - 50} more")

    # Aggregate proposals across all records
    ticker_props: dict[str, dict] = {}
    for r in records:
        for p in r.get("proposed_new_tickers", []):
            t = p.get("ticker")
            if t and t not in ticker_props:
                ticker_props[t] = p
    macro_author_props: dict[str, dict] = {}
    for r in records:
        for p in r.get("proposed_new_macro_authors", []):
            n = p.get("name")
            if n and n not in macro_author_props:
                macro_author_props[n] = p
    semis_author_props: dict[str, dict] = {}
    for r in records:
        for p in r.get("proposed_new_semis_authors", []):
            n = p.get("name")
            if n and n not in semis_author_props:
                semis_author_props[n] = p
    theme_props: dict[str, list[str]] = {}  # theme → files that wanted it
    for r in records:
        for t in r.get("proposed_new_themes", []):
            theme_props.setdefault(t, []).append(r["file"])

    if ticker_props:
        verb = "WOULD ADD" if dry_run else "ADDED"
        lines.append(f"\nNew tickers — {verb} ({len(ticker_props)}):")
        for t, p in sorted(ticker_props.items()):
            lines.append(f"  - {t} ({p.get('market', '?')}) — {p.get('name', '')}")

    if macro_author_props:
        verb = "WOULD ADD" if dry_run else "ADDED"
        lines.append(f"\nNew MACRO authors — {verb} ({len(macro_author_props)}):")
        for n in sorted(macro_author_props.keys()):
            lines.append(f"  - {n}")

    if semis_author_props:
        verb = "WOULD ADD" if dry_run else "ADDED"
        lines.append(f"\nNew SEMIS authors — {verb} ({len(semis_author_props)}):")
        for n in sorted(semis_author_props.keys()):
            lines.append(f"  - {n}")

    if theme_props:
        lines.append(f"\nNew themes — PENDING YOUR APPROVAL ({len(theme_props)}):")
        for theme, files in sorted(theme_props.items(), key=lambda x: -len(x[1])):
            lines.append(f"  - {theme}  ({len(files)} file(s))")
            for f in files[:3]:
                lines.append(f"      · {f}")
            if len(files) > 3:
                lines.append(f"      · ... and {len(files) - 3} more")

    low_conf = [r for r in records if r.get("confidence") == "low"]
    if low_conf:
        lines.append(f"\nLow-confidence classifications ({len(low_conf)}) — review:")
        for r in low_conf[:20]:
            lines.append(f"  - {r['file']} -> {r['primary_type']}/{r['primary_subject']}")

    if failed:
        lines.append(f"\nFailed:")
        for f in failed[:20]:
            lines.append(f"  - {f['file']}: {f['error']}")
        if len(failed) > 20:
            lines.append(f"  ... and {len(failed) - 20} more")

    return "\n".join(lines)


def _telegram_notify(text: str) -> None:
    """Send a one-shot Telegram message to the allowlisted users."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    raw_ids = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if not token or not raw_ids:
        return
    import requests
    ids = [int(x) for x in raw_ids.split(",") if x.strip()]
    # Truncate to fit Telegram's 4096-char limit
    body = text if len(text) <= 3800 else text[:3800] + "\n... (truncated)"
    for uid in ids:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": uid, "text": body},
                timeout=10,
            )
        except Exception as e:
            print(f"  Telegram notification failed for {uid}: {e}")


# ---------------------------------------------------------------------------
# Main entry — called from main.py
# ---------------------------------------------------------------------------

def bulk_ingest(folder: str, dry_run: bool = False, limit: int = 0,
                with_cross_cut: bool = False, force: bool = False,
                notify: bool = True) -> None:
    """Process every PDF in `folder`.

    folder: path to a directory of PDFs (recursive)
    dry_run: just triage and report classifications, don't store
    limit: process at most this many new files (0 = unlimited)
    with_cross_cut: also run cross-cutting analysis per file (expensive)
    force: re-process files already in state
    notify: send Telegram summary at end
    """
    import click

    folder_path = Path(folder).expanduser().resolve()
    if not folder_path.exists() or not folder_path.is_dir():
        click.echo(f"Folder not found or not a directory: {folder_path}")
        raise SystemExit(1)

    pdfs = _scan_pdfs(folder_path)
    if not pdfs:
        click.echo(f"No PDFs found in {folder_path}")
        return

    state = _load_state()
    processed_hashes = set(state.get("processed", {}).keys())
    failed_state = state.get("failed", {})
    MAX_RETRIES = 3

    queue = []
    skipped = 0
    skipped_gave_up = 0
    for pdf in pdfs:
        h = _hash_file(pdf)
        if h in processed_hashes and not force:
            skipped += 1
            continue
        # Retry cap: don't keep retrying a file that has hit MAX_RETRIES failures.
        fail_rec = failed_state.get(h)
        if fail_rec and not force and fail_rec.get("attempts", 0) >= MAX_RETRIES:
            skipped_gave_up += 1
            continue
        queue.append((pdf, h))

    if limit and limit > 0:
        queue = queue[:limit]

    click.echo(f"Folder: {folder_path}")
    click.echo(f"PDFs found: {len(pdfs)}  |  Already processed: {skipped}  |  Gave-up: {skipped_gave_up}  |  Queued: {len(queue)}")
    if dry_run:
        click.echo("DRY RUN — no storage, just triage")
    if with_cross_cut:
        click.echo("Cross-cutting analysis: ON  (2-5x cost/time)")
    click.echo("")

    if not queue:
        click.echo("Nothing to do.")
        return

    run_start = time.time()
    records: list[dict] = []
    failed: list[dict] = []

    # Single shared client so prompt-cache hits persist across files.
    from anthropic import Anthropic
    shared_client = Anthropic(max_retries=3, timeout=600.0)

    for idx, (pdf, h) in enumerate(queue, start=1):
        click.echo(f"[{idx}/{len(queue)}] {pdf.name}  ({pdf.stat().st_size / 1024 / 1024:.1f} MB)")
        try:
            rec = _process_one(pdf, dry_run=dry_run, with_cross_cut=with_cross_cut, client=shared_client)
            rec["hash"] = h
            rec["path"] = str(pdf)
            records.append(rec)

            click.echo(
                f"    → {rec['primary_type']}/{rec['primary_subject']}  "
                f"({rec.get('total_seconds', rec['triage_seconds'])}s, conf={rec['confidence']})"
            )
            if rec["tickers_covered"]:
                click.echo(f"    tickers: {', '.join(rec['tickers_covered'])}")
            if rec["themes_touched"]:
                click.echo(f"    themes:  {', '.join(rec['themes_touched'])}")

            if not dry_run:
                state.setdefault("processed", {})[h] = {
                    "file": pdf.name,
                    "source_path": str(pdf),
                    "primary_type": rec["primary_type"],
                    "primary_subject": rec["primary_subject"],
                    "tickers_covered": rec["tickers_covered"],
                    "themes_touched": rec["themes_touched"],
                    "held_for_review": rec.get("held_for_review", False),
                    "processed_at": datetime.now().isoformat(timespec="seconds"),
                }
                # Clear any prior failure record on success (or held)
                state.get("failed", {}).pop(h, None)
                _save_state(state)
        except KeyboardInterrupt:
            click.echo("\nInterrupted. State saved; re-run to resume.")
            raise
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            tb = traceback.format_exc()
            click.echo(f"    ✗ FAILED: {err}")
            failed.append({"file": pdf.name, "path": str(pdf), "error": err, "trace": tb})
            prior = state.setdefault("failed", {}).get(h, {})
            attempts = int(prior.get("attempts", 0)) + 1
            state["failed"][h] = {
                "file": pdf.name,
                "error": err,
                "attempts": attempts,
                "first_failed_at": prior.get("first_failed_at") or datetime.now().isoformat(timespec="seconds"),
                "last_failed_at": datetime.now().isoformat(timespec="seconds"),
            }
            _save_state(state)

    elapsed = time.time() - run_start

    state.setdefault("runs", []).append({
        "started_at": datetime.fromtimestamp(run_start).isoformat(timespec="seconds"),
        "folder": str(folder_path),
        "dry_run": dry_run,
        "with_cross_cut": with_cross_cut,
        "queued": len(queue),
        "succeeded": len(records),
        "failed": len(failed),
        "elapsed_seconds": round(elapsed, 1),
    })
    _save_state(state)

    summary = _format_summary(records, failed, elapsed, dry_run=dry_run)
    click.echo("\n" + summary)

    if notify and not dry_run:
        _telegram_notify(summary)
