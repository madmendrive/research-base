"""Daily combined sweep-digest coordinator.

The scheduled 03:00 HKT Gmail email sweep and research-inbox folder scan each
submit their existing-format digest text here (instead of sending it directly).
When both expected parts are in, ONE merged Telegram message is sent. A failsafe
flush sends whatever arrived if the other sweep stalls or fails.

Both sweeps run in the single serial heavy-worker lane, so part submissions
never race. State lives in data/_combined_sweep_digest.json.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.notify import telegram_send_markdownish_html

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = PROJECT_ROOT / "data" / "_combined_sweep_digest.json"

EXPECTED = ("email", "inbox")
LABELS = {"email": "Research email sweep", "inbox": "Inbox scan"}
SEPARATOR = "\n\n———\n\n"
STALE_MINUTES = 20
_TZ = ZoneInfo("Asia/Hong_Kong")


def _today() -> str:
    return datetime.now(_TZ).strftime("%Y-%m-%d")


def _load() -> dict | None:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _save(rec: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")


def begin_day(date: str | None = None) -> None:
    """Open today's coordinator. Idempotent within a day; resets a stale prior day."""
    date = date or _today()
    rec = _load()
    if rec and rec.get("date") == date:
        return
    _save({
        "date": date,
        "parts": {},
        "sent": False,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })


def submit_part(part: str, text: str, date: str | None = None) -> bool:
    """Record one sweep's digest text; send the combined message once both
    expected parts are present. Returns True if the combined message was sent."""
    date = date or _today()
    rec = _load()
    if not rec or rec.get("date") != date:
        begin_day(date)
        rec = _load() or {}
    if rec.get("sent"):
        return False
    rec.setdefault("parts", {})[part] = text or ""
    _save(rec)
    if all(p in rec["parts"] for p in EXPECTED):
        _send(rec)
        return True
    return False


def flush_if_stale(date: str | None = None) -> bool:
    """Failsafe: if today's coordinator is unsent and older than STALE_MINUTES,
    send whatever arrived — even with zero submitted parts — so a sweep that
    crashed (or whose ingests failed) can't leave you with silence. Returns True
    if it sent."""
    rec = _load()
    if not rec or rec.get("sent"):
        return False
    try:
        created = datetime.fromisoformat(rec["created_at"])
    except (KeyError, ValueError):
        return False
    age_min = (datetime.now(timezone.utc) - created).total_seconds() / 60
    if age_min < STALE_MINUTES:
        return False
    _send(rec, partial=True)
    return True


def _recover_inbox_section() -> str:
    """Best-effort inbox digest from the ingests that DID succeed today, for when
    the inbox part was never submitted (e.g. some ingest_file jobs failed, so the
    'all files done' count was never reached)."""
    try:
        from scripts.folder_scan import LATEST_SCAN_PATH, format_scan_digest

        if not LATEST_SCAN_PATH.exists():
            return ""
        scan = json.loads(LATEST_SCAN_PATH.read_text(encoding="utf-8"))
        if not scan.get("combined"):
            return ""
        items = scan.get("items") or []
        if not items:
            return ""
        text = format_scan_digest(items)
        total = int(scan.get("total") or 0)
        if total and len(items) < total:
            text += (f"\n\n(Partial: {len(items)} of {total} files ingested; the rest "
                     f"failed or are still processing — check worker logs.)")
        return text
    except Exception:
        log.exception("inbox digest recovery failed")
        return ""


def _send(rec: dict, *, partial: bool = False) -> None:
    parts = dict(rec.get("parts") or {})
    if partial and not parts.get("inbox"):
        recovered = _recover_inbox_section()
        if recovered:
            parts["inbox"] = recovered
    sections: list[str] = []
    for key in EXPECTED:
        if parts.get(key):
            sections.append(parts[key])
        elif partial:
            sections.append(f"**{LABELS[key]} — did not complete today** (check worker logs).")
    message = SEPARATOR.join(sections)
    if message:
        try:
            telegram_send_markdownish_html(message)
        except Exception:
            log.exception("combined digest send failed")
    rec["sent"] = True
    rec["sent_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _save(rec)
