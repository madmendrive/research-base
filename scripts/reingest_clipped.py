"""Find and queue re-ingestion of documents clipped by the old extraction ceiling.

Until 2026-06, structured extraction only read the first 30,000 characters of
a document. Anything longer was ingested from a clipped read — summaries,
estimates, and structured memory all missed the tail. This tool finds those
PDFs in the inbox and clears their hashes from the ingest state files so the
next bulk-ingest / folder sweep re-processes exactly them (and nothing else).

CLI: python main.py reingest-clipped [--folder PATH] [--threshold N] [--apply]
"""

import hashlib
import json
from pathlib import Path

from scripts.fileio import write_json_atomic

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
BULK_STATE = DATA_DIR / "_bulk_ingest_state.json"
SWEEPER_STATE = DATA_DIR / "_sweeper_state.json"
FAST_STATE = DATA_DIR / "_fast_ingest_state.json"
STAGING_DIR = DATA_DIR / "_staging_ingest"

OLD_CAP_CHARS = 30_000
DEFAULT_FOLDER = Path(r"C:\Users\Owner\Downloads\research-inbox")


def _full_hash(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def _text_length(path: Path) -> int | None:
    from scripts.classifier import extract_text

    try:
        text, err = extract_text(path, max_pages=300, max_chars=None)
    except Exception:
        return None
    if err or not text:
        return None
    return len(text)


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _clear_hashes(state_path: Path, keys: set[str], buckets: tuple[str, ...]) -> int:
    state = _load(state_path)
    if not state:
        return 0
    removed = 0
    for bucket in buckets:
        entries = state.get(bucket)
        if not isinstance(entries, dict):
            continue
        for key in keys & set(entries.keys()):
            entries.pop(key)
            removed += 1
    if removed:
        write_json_atomic(state_path, state, trailing_newline=False)
    return removed


# UTC instant of the schema-evolution commit (f0853a4, 2026-06-10 14:24:38
# HKT). Stage records older than this were extracted with the pre-enrichment
# schema (no segment_estimates / valuation / industry_assumptions /
# primer_concepts fields).
SCHEMA_CUTOFF_UTC = "2026-06-10T06:24:38+00:00"


def reingest_stale_schema(cutoff: str = SCHEMA_CUTOFF_UTC, apply: bool = False) -> dict:
    """Queue re-ingestion of docs whose staged extraction pre-dates the
    enriched schema. Clears their state hashes and staging records so the
    next bulk-ingest-fast re-processes exactly them."""
    stale = []
    for stage in sorted(STAGING_DIR.glob("*.json")):
        try:
            record = json.loads(stage.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        staged_at = str(record.get("staged_at") or "")
        if staged_at and staged_at < cutoff:
            stale.append({"stage": stage, "digest": record.get("digest") or stage.stem,
                          "file": record.get("file"), "staged_at": staged_at})

    stats = {
        "cutoff": cutoff,
        "stale": len(stale),
        "files": [{"file": s["file"], "staged_at": s["staged_at"]} for s in stale],
        "applied": False,
    }
    if not apply or not stale:
        return stats

    full_hashes = {s["digest"] for s in stale}
    short_hashes = {h[:16] for h in full_hashes}
    cleared = 0
    cleared += _clear_hashes(BULK_STATE, short_hashes, ("processed", "failed"))
    cleared += _clear_hashes(SWEEPER_STATE, short_hashes, ("processed", "failed"))
    cleared += _clear_hashes(FAST_STATE, full_hashes, ("committed", "failed"))
    for s in stale:
        s["stage"].unlink(missing_ok=True)
    stats.update({"applied": True, "state_entries_cleared": cleared,
                  "stage_files_removed": len(stale)})
    return stats


def find_clipped(folder: Path, threshold: int = OLD_CAP_CHARS) -> list[dict]:
    candidates = []
    for pdf in sorted(folder.rglob("*.pdf")):
        if not pdf.is_file() or pdf.name.startswith("."):
            continue
        length = _text_length(pdf)
        if length is None:
            candidates.append({"file": pdf, "chars": None, "clipped": False, "error": True})
            continue
        candidates.append({"file": pdf, "chars": length, "clipped": length > threshold, "error": False})
    return candidates


def reingest_clipped(folder: Path | None = None, threshold: int = OLD_CAP_CHARS,
                     apply: bool = False) -> dict:
    folder = Path(folder) if folder else DEFAULT_FOLDER
    if not folder.is_dir():
        raise FileNotFoundError(f"Folder not found: {folder}")

    scanned = find_clipped(folder, threshold)
    clipped = [c for c in scanned if c["clipped"]]
    stats = {
        "folder": str(folder),
        "threshold": threshold,
        "scanned": len(scanned),
        "unreadable": sum(1 for c in scanned if c["error"]),
        "clipped": len(clipped),
        "files": [{"file": c["file"].name, "chars": c["chars"]} for c in clipped],
        "applied": False,
    }
    if not apply or not clipped:
        return stats

    full_hashes = {_full_hash(c["file"]) for c in clipped}
    short_hashes = {h[:16] for h in full_hashes}

    cleared = 0
    cleared += _clear_hashes(BULK_STATE, short_hashes, ("processed", "failed"))
    cleared += _clear_hashes(SWEEPER_STATE, short_hashes, ("processed", "failed"))
    cleared += _clear_hashes(FAST_STATE, full_hashes, ("committed", "failed"))

    stale_stages = 0
    for digest in full_hashes:
        stage = STAGING_DIR / f"{digest}.json"
        if stage.exists():
            stage.unlink()
            stale_stages += 1

    stats.update({"applied": True, "state_entries_cleared": cleared, "stage_files_removed": stale_stages})
    return stats
