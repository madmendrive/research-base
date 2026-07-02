"""Scan an inbox folder and enqueue PDFs for the single-writer worker."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from scripts.jobs import enqueue_job, job_for_dedupe_key
from scripts.notify import telegram_send, telegram_send_markdownish_html

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LATEST_SCAN_PATH = DATA_DIR / "_latest_inbox_scan.json"

IGNORE_PATTERNS = (
    ".syncthing.",
    "~syncthing~",
    ".crdownload",
    ".part",
    ".download",
    ".tmp",
)


def _is_temp_file(path: Path) -> bool:
    name = path.name.lower()
    if name.startswith(".") or name.startswith("~"):
        return True
    return any(p in name for p in IGNORE_PATTERNS)


def _hash_file(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Scan digest state — one JSON file tracks the latest folder-scan run
# ---------------------------------------------------------------------------

def _save_scan_session(scan_id: str, total: int, combined: bool = False) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "scan_id": scan_id,
        "total": total,
        "combined": combined,
        "items": [],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    LATEST_SCAN_PATH.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def append_scan_item(scan_id: str, item: dict) -> tuple[int, int]:
    """Append a completed ingest result to the latest scan digest.

    Returns (current_item_count, total_expected).
    Called by the ingest_file job handler after each file finishes.
    Safe for the serial heavy worker (no concurrent writers).
    """
    if not LATEST_SCAN_PATH.exists():
        return 0, 0
    try:
        record = json.loads(LATEST_SCAN_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0, 0
    if record.get("scan_id") != scan_id:
        return 0, 0
    items = record.get("items") or []
    item = dict(item)
    item["rank"] = len(items) + 1
    items.append(item)
    record["items"] = items
    LATEST_SCAN_PATH.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(items), int(record.get("total", 0))


def get_scan_item_by_rank(rank: int) -> dict | None:
    """Look up one item from the latest inbox scan digest by rank (1-indexed)."""
    if not LATEST_SCAN_PATH.exists():
        return None
    try:
        record = json.loads(LATEST_SCAN_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    for item in record.get("items", []):
        try:
            if int(item.get("rank", 0)) == int(rank):
                return item
        except Exception:
            continue
    return None


def format_scan_digest(items: list[dict]) -> str:
    """Inbox-scan digest text. Mirrors the email-sweep digest: rank, bold title,
    author, /inbox_N command.

    Uses **bold** (not literal <b>): telegram_html escapes raw tags, so the old
    <b> markup was rendered verbatim instead of bold. The layout is unchanged.
    """
    if not items:
        return "**Inbox scan — 0 new file(s)**\n(no new files today)"

    def _as_text(v) -> str:
        # title/author can come back from triage as a list of names
        if isinstance(v, (list, tuple)):
            return ", ".join(str(x).strip() for x in v if x)
        return str(v or "").strip()

    lines = [f"**Inbox scan — {len(items)} new file(s)**", ""]
    for it in items:
        title = _as_text(it.get("title")) or "(untitled)"
        author = _as_text(it.get("author"))
        rank = it.get("rank", "?")
        lines.append(f"{rank}. **{title}**")
        if author:
            lines.append(f"   {author}")
        lines.append(f"   analyse: /inbox_{rank}")
        lines.append("")
    return "\n".join(lines).strip()


def send_scan_digest(items: list[dict]) -> None:
    """Send the inbox-scan digest to Telegram (standalone, non-combined runs)."""
    if not items:
        return
    telegram_send_markdownish_html(format_scan_digest(items))


# ---------------------------------------------------------------------------
# Folder scanner
# ---------------------------------------------------------------------------

def folder_scan(folder: str, notify: bool = False, recursive: bool = False,
                analyse: bool = False, combined: bool = False) -> dict:
    base = Path(folder).expanduser().resolve()
    if not base.exists() or not base.is_dir():
        raise FileNotFoundError(base)
    globber = base.rglob if recursive else base.glob
    scanned = 0
    queued = 0
    already_seen = 0
    skipped = 0

    # Generate a scan_id up front so every ingest_file job in this run
    # shares it; the last one to complete will fire the digest.
    scan_id = datetime.now().strftime("%Y%m%d_%H%M%S") if notify else None

    for pdf in sorted([*globber("*.pdf"), *globber("*.txt")]):
        if not pdf.is_file() or _is_temp_file(pdf):
            skipped += 1
            continue
        scanned += 1
        digest = _hash_file(pdf)
        dedupe_key = f"ingest_file:{digest}"
        if job_for_dedupe_key(dedupe_key):
            already_seen += 1
            continue
        job_payload: dict = {"path": str(pdf), "notify": analyse}
        if scan_id:
            job_payload["scan_id"] = scan_id
        job_id = enqueue_job(
            "ingest_file",
            job_payload,
            dedupe_key=dedupe_key,
        )
        if job_id:
            queued += 1

    stats = {
        "folder": str(base),
        "scanned": scanned,
        "queued": queued,
        "already_seen": already_seen,
        "skipped": skipped,
    }
    if notify:
        if queued > 0:
            _save_scan_session(scan_id, queued, combined=combined)
            # In combined mode the merged digest is sent once the last ingest
            # finishes; skip the interim "digest will follow" ping.
            if not combined:
                telegram_send(
                    f"Inbox scan: {scanned} scanned, {queued} new file(s) queued. "
                    f"Digest will follow when ingestion completes."
                )
        elif combined:
            # Nothing new, but the combined message still needs an inbox part.
            from scripts.combined_digest import submit_part
            submit_part("inbox", format_scan_digest([]))
        else:
            telegram_send(
                f"Inbox scan: {scanned} scanned, nothing new "
                f"({already_seen} already seen, {skipped} skipped)."
            )
    return stats
