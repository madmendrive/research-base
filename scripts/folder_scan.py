"""Scan an inbox folder and enqueue PDFs for the single-writer worker."""

from __future__ import annotations

import hashlib
from pathlib import Path

from scripts.jobs import enqueue_job
from scripts.notify import telegram_send

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


def folder_scan(folder: str, notify: bool = False, recursive: bool = False,
                analyse: bool = False) -> dict:
    base = Path(folder).expanduser().resolve()
    if not base.exists() or not base.is_dir():
        raise FileNotFoundError(base)
    globber = base.rglob if recursive else base.glob
    queued = 0
    skipped = 0
    for pdf in sorted(globber("*.pdf")):
        if not pdf.is_file() or _is_temp_file(pdf):
            skipped += 1
            continue
        digest = _hash_file(pdf)
        job_id = enqueue_job(
            "ingest_file",
            {"path": str(pdf), "notify": analyse},
            dedupe_key=f"ingest_file:{digest}",
        )
        if job_id:
            queued += 1
    stats = {"folder": str(base), "queued": queued, "skipped": skipped}
    if notify:
        telegram_send(f"Folder scan queued {queued} PDF(s) from {base}")
    return stats
