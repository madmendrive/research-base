"""Shared file-write helpers.

Summary/state JSON files are read concurrently by the bot, sweeper, and
heartbeat, so writers must go through an atomic tmp+rename to avoid a reader
catching a half-written file.
"""

from __future__ import annotations

import json
from pathlib import Path


MAX_TEXT_CHARS = 400_000

_json_cache: dict[str, tuple[tuple[float, int], dict]] = {}


def load_json_cached(path: Path) -> dict:
    """Read a JSON file, cached on (mtime, size). Returns {} if missing/invalid.

    Lets the sweeper and bulk-ingest consult each other's state file on every
    skip-check without re-parsing an unchanged file.
    """
    path = Path(path)
    key = str(path)
    try:
        stat = path.stat()
    except OSError:
        _json_cache.pop(key, None)
        return {}
    stamp = (stat.st_mtime, stat.st_size)
    cached = _json_cache.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    _json_cache[key] = (stamp, data)
    return data


def extract_file_text(path: Path, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    """Extract text from a research file (PDF, HTML, or plain text).

    Shared by research/macro/thematic (was triplicated there, with macro
    missing .tsv support).
    """
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix in (".pdf", ".htm", ".html"):
        from scripts.classifier import extract_text

        text, err = extract_text(path, max_pages=300, max_chars=max_chars)
        if err:
            raise RuntimeError(f"Failed to extract text from {path.name}: {err}")
        if not text:
            raise RuntimeError(f"No text extracted from {path.name}")
        return text
    if suffix in (".txt", ".md", ".csv", ".tsv"):
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        if len(text) > max_chars:
            text = text[:max_chars] + "\n[...truncated]"
        return text
    raise RuntimeError(f"Unsupported file type: {suffix}")


def pdf_page_count(path: Path) -> int:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        return len(pdf.pages)


def write_json_atomic(path: Path, data, *, indent: int = 2, trailing_newline: bool = True) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=indent, ensure_ascii=False)
    if trailing_newline:
        text += "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
