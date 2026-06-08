"""Ops helpers for notes, pending-review files, and worker actions."""

from __future__ import annotations

import hashlib
import shutil
from datetime import datetime
from pathlib import Path

from scripts import kb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
NOTES_DIR = DATA_DIR / "_notes"
PENDING_DIR = DATA_DIR / "_pending_review"


def store_user_note(target: str, text: str, author: str = "Telegram") -> dict:
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    target = target.strip()
    if not target:
        raise ValueError("target is required")
    if not text.strip():
        raise ValueError("note text is required")
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    title = f"{target} note {stamp}"
    dest = NOTES_DIR / f"{stamp}_{kb.slugify(target)}.md"
    body = (
        f"# {title}\n\n"
        f"- Source: user note\n"
        f"- Target: {target}\n"
        f"- Author: {author}\n"
        f"- Created: {datetime.now().isoformat(timespec='seconds')}\n\n"
        f"{text.strip()}\n"
    )
    dest.write_text(body, encoding="utf-8")
    result = kb.index_text(
        title=title,
        text=body,
        source_type="note",
        source_uri=f"note:{dest.name}",
        source_path=str(dest),
        metadata={"target": target, "author": author},
        force=True,
    )
    return {"path": str(dest), **result}


def pending_token(path: Path) -> str:
    return hashlib.sha256(path.name.encode("utf-8", errors="replace")).hexdigest()[:12]


def find_pending_by_token(token: str) -> Path | None:
    if not PENDING_DIR.exists():
        return None
    for path in PENDING_DIR.glob("*.pdf"):
        if pending_token(path) == token:
            return path
    return None


def list_pending(limit: int = 10) -> list[dict]:
    if not PENDING_DIR.exists():
        return []
    rows = []
    for path in sorted(PENDING_DIR.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        rows.append({
            "file": path.name,
            "path": str(path),
            "token": pending_token(path),
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        })
    return rows


def confirm_pending(path: str | Path) -> dict:
    """Accept the current best-guess triage for a pending file and store it."""
    from scripts.bot_pipeline import _store_primary
    from scripts.triage import triage_document

    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(src)
    triage = triage_document(src)
    if triage.get("confidence") == "low":
        triage["confidence"] = "medium"
        triage["rationale"] = (triage.get("rationale", "") + " Confirmed manually from pending review.").strip()
    report = _store_primary(triage, src)
    kb.index_file(src, source_type="research", metadata={"pending_confirmed": True, "triage": triage})
    confirmed_dir = PENDING_DIR / "confirmed"
    confirmed_dir.mkdir(parents=True, exist_ok=True)
    dest = confirmed_dir / src.name
    if dest.exists():
        dest = confirmed_dir / f"{datetime.now().strftime('%H%M%S')}_{src.name}"
    shutil.move(str(src), str(dest))
    return {"file": src.name, "stored_as": triage.get("primary_subject"), "report": report[:2000]}


def drop_pending(path: str | Path) -> dict:
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(src)
    dropped_dir = PENDING_DIR / "dropped"
    dropped_dir.mkdir(parents=True, exist_ok=True)
    dest = dropped_dir / src.name
    if dest.exists():
        dest = dropped_dir / f"{datetime.now().strftime('%H%M%S')}_{src.name}"
    shutil.move(str(src), str(dest))
    return {"file": src.name, "dropped_to": str(dest)}
