"""Daily combined sweep-digest coordinator.

The scheduled 03:00 HKT Gmail email sweep and research-inbox folder scan each
submit their existing-format digest text here (instead of sending it directly).
When both expected parts are in, ONE merged Telegram message is sent. A failsafe
flush sends whatever arrived if the other sweep stalls or fails.

Concurrency: submit_part runs in the heavy worker while flush_if_stale runs in
the heartbeat process, so every load-modify-save of the state file happens under
a cross-process lockfile, writes are atomic (tmp+replace), and sending is
claim-first: the record is marked sent under the lock BEFORE the Telegram call,
so two processes can never both send. A part that lands after a failsafe flush
is sent as a labelled follow-up instead of being discarded.
State lives in data/_combined_sweep_digest.json.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.notify import telegram_send_markdownish_html

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = PROJECT_ROOT / "data" / "_combined_sweep_digest.json"
LOCK_PATH = STATE_PATH.with_suffix(".lock")

EXPECTED = ("email", "inbox")
LABELS = {"email": "Research email sweep", "inbox": "Inbox scan"}
SEPARATOR = "\n\n———\n\n"
# The inbox part only arrives after every ingest_file job finishes, and those
# queue behind the 03:00 reindexes in the serial heavy lane — 20 minutes was
# routinely too tight. Staleness is also measured from the LAST activity
# (created_at or newest part submission), not from enqueue time.
STALE_MINUTES = int(os.environ.get("COMBINED_DIGEST_STALE_MINUTES", "90"))
_TZ = ZoneInfo("Asia/Hong_Kong")

_LOCK_TIMEOUT_S = 10.0
_LOCK_STALE_S = 60.0


def _today() -> str:
    return datetime.now(_TZ).strftime("%Y-%m-%d")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _state_lock():
    """Cross-process mutual exclusion via O_CREAT|O_EXCL lockfile.

    Held only around load-modify-save (never across network calls). A lock
    older than _LOCK_STALE_S is treated as abandoned. On timeout we log and
    proceed unlocked — a degraded submit beats a failed sweep job.
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    acquired = False
    while True:
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
            acquired = True
            break
        except FileExistsError:
            try:
                if time.time() - LOCK_PATH.stat().st_mtime > _LOCK_STALE_S:
                    LOCK_PATH.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                log.error("combined digest lock timed out; proceeding unlocked")
                break
            time.sleep(0.2)
    try:
        yield
    finally:
        if acquired:
            try:
                LOCK_PATH.unlink(missing_ok=True)
            except OSError:
                pass


def _load() -> dict | None:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("combined digest state unreadable; starting fresh", exc_info=True)
        return None


def _save(rec: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _new_rec(date: str) -> dict:
    return {
        "date": date,
        "parts": {},
        "sent": False,
        "sent_parts": [],
        "created_at": _utcnow(),
        "updated_at": _utcnow(),
    }


def begin_day(date: str | None = None) -> None:
    """Open today's coordinator. Idempotent within a day; resets a stale prior day."""
    date = date or _today()
    with _state_lock():
        rec = _load()
        if rec and rec.get("date") == date:
            return
        _save(_new_rec(date))


def submit_part(part: str, text: str, date: str | None = None) -> bool:
    """Record one sweep's digest text; send the combined message once both
    expected parts are present. If the failsafe already flushed, a non-empty
    late part is sent as a labelled follow-up. Returns True if the combined
    message was sent."""
    date = date or _today()
    to_send: str | None = None
    combined = False
    with _state_lock():
        rec = _load()
        if not rec or rec.get("date") != date:
            rec = _new_rec(date)
        rec.setdefault("parts", {})[part] = text or ""
        rec["updated_at"] = _utcnow()
        if rec.get("sent"):
            sent_parts = rec.setdefault("sent_parts", [])
            if part not in sent_parts and (text or "").strip():
                # Failsafe flushed before this part landed: send it late
                # rather than silently discarding the completed digest.
                sent_parts.append(part)
                to_send = f"**{LABELS.get(part, part)} (late)**{SEPARATOR}{text}"
        elif all(p in rec["parts"] for p in EXPECTED):
            to_send = _compose(rec["parts"], partial=False)
            combined = True
            rec["sent"] = True
            rec["sent_parts"] = [p for p in EXPECTED if (rec["parts"].get(p) or "").strip()]
            rec["sent_at"] = _utcnow()
        _save(rec)
    if to_send:
        _deliver(to_send)
    return combined


def flush_if_stale(date: str | None = None) -> bool:
    """Failsafe: if today's coordinator is unsent and idle longer than
    STALE_MINUTES, send whatever arrived — even with zero submitted parts — so
    a sweep that crashed (or whose ingests failed) can't leave you with
    silence. Late-arriving parts are still delivered afterwards as follow-ups.
    Returns True if it sent."""
    to_send: str | None = None
    with _state_lock():
        rec = _load()
        if not rec or rec.get("sent"):
            return False
        try:
            last_activity = datetime.fromisoformat(
                rec.get("updated_at") or rec["created_at"]
            )
        except (KeyError, ValueError):
            return False
        idle_min = (datetime.now(timezone.utc) - last_activity).total_seconds() / 60
        if idle_min < STALE_MINUTES:
            return False
        parts = dict(rec.get("parts") or {})
        recovered_inbox = False
        if not parts.get("inbox"):
            recovered = _recover_inbox_section()
            if recovered:
                parts["inbox"] = recovered
                recovered_inbox = True
        to_send = _compose(parts, partial=True)
        rec["sent"] = True
        # A recovered inbox section is best-effort partial: leave "inbox" out
        # of sent_parts so the real part still goes out late if it completes.
        rec["sent_parts"] = [
            p for p in EXPECTED
            if (rec.get("parts", {}).get(p) or "").strip()
            and not (p == "inbox" and recovered_inbox)
        ]
        rec["sent_at"] = _utcnow()
        _save(rec)
    if to_send:
        _deliver(to_send)
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


def _compose(parts: dict, *, partial: bool) -> str:
    sections: list[str] = []
    for key in EXPECTED:
        if parts.get(key):
            sections.append(parts[key])
        elif partial:
            sections.append(
                f"**{LABELS[key]} — not finished yet** "
                f"(it will be sent separately if it completes; check worker logs otherwise)."
            )
    return SEPARATOR.join(sections)


def _deliver(message: str) -> None:
    """Send outside the lock; record the outcome for postmortem."""
    delivered = False
    try:
        delivered = bool(telegram_send_markdownish_html(message))
        from scripts.notify import discord_send

        discord_send("research_sweep", message)
    except Exception:
        log.exception("combined digest send failed")
    if not delivered:
        log.error("combined digest was NOT delivered (see notify errors above)")
    with _state_lock():
        rec = _load()
        if rec:
            rec["last_delivery_ok"] = delivered
            rec["last_delivery_at"] = _utcnow()
            _save(rec)
