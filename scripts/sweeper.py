"""Folder sweeper — watches an inbox for new PDFs and ingests them.

Long-running process. Run on whichever machine should host the always-on
ingest pipeline (production: Mac mini via LaunchAgent; testing: Windows
desktop in a foreground terminal).

CLI: python main.py sweep [--folder PATH] [--no-cross-cut]
"""

import hashlib
import json
import os
import time
import traceback
from datetime import datetime
from pathlib import Path

import requests
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SWEEPER_STATE_PATH = DATA_DIR / "_sweeper_state.json"
BULK_STATE_PATH = DATA_DIR / "_bulk_ingest_state.json"

# Settle window mechanics. Syncthing writes to ~syncthing~*.tmp / .syncthing.*.tmp
# and atomically renames on completion, so on_moved with a non-hidden .pdf
# destination is itself a completion signal — we keep a short size-stability
# check as belt-and-suspenders, but it doesn't need to be long.
SETTLE_INTERVAL_S = 1.0
SETTLE_REQUIRED_PASSES = 2   # ~2s of no growth — atomic rename is already proof of completeness
SETTLE_MAX_WAIT_S = 300      # absolute ceiling

# Patterns to ignore on create events (Syncthing/browser temp files)
IGNORE_PATTERNS = (
    ".syncthing.",     # Syncthing temp prefix
    "~syncthing~",     # Syncthing alt prefix
    ".crdownload",     # Chrome partial download
    ".part",           # Firefox partial download
    ".download",       # Safari partial download
    ".tmp",            # generic
)


def _is_temp_file(path: Path) -> bool:
    name = path.name.lower()
    if name.startswith(".") or name.startswith("~"):
        return True
    return any(p in name for p in IGNORE_PATTERNS)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _load_state(path: Path) -> dict:
    if not path.exists():
        return {"processed": {}, "failed": {}}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {"processed": {}, "failed": {}}


def _save_state(state: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def _bulk_hashes() -> set[str]:
    """Hashes of files already processed by the bulk-ingest pass — skip these.

    Re-read (mtime-cached) on every check so a bulk run that finishes while
    the sweeper is already running is still seen; a startup-only snapshot
    used to double-process anything bulk handled after the sweeper started.
    """
    from scripts.fileio import load_json_cached

    return set(load_json_cached(BULK_STATE_PATH).get("processed", {}).keys())


def _hash_file(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Telegram push
# ---------------------------------------------------------------------------

def _chunks(text: str, size: int = 3800) -> list[str]:
    if len(text) <= size:
        return [text]
    out, buf = [], ""
    for para in text.split("\n\n"):
        if len(para) > size:
            if buf:
                out.append(buf)
                buf = ""
            for i in range(0, len(para), size):
                out.append(para[i:i + size])
            continue
        if len(buf) + len(para) + 2 > size:
            out.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        out.append(buf)
    return out


class TelegramPusher:
    def __init__(self, token: str | None, user_ids: list[int], log):
        self.token = token
        self.user_ids = user_ids
        self.log = log

    def send(self, text: str) -> None:
        if not self.token or not self.user_ids:
            self.log.info("(no telegram configured) %s", text[:200])
            return
        for piece in _chunks(text):
            for uid in self.user_ids:
                try:
                    r = requests.post(
                        f"https://api.telegram.org/bot{self.token}/sendMessage",
                        data={"chat_id": uid, "text": piece},
                        timeout=15,
                    )
                    if r.status_code != 200:
                        self.log.warning("telegram %d for %s: %s", r.status_code, uid, r.text[:200])
                except Exception as e:
                    self.log.warning("telegram send failed for %s: %s", uid, e)


# ---------------------------------------------------------------------------
# File stability check
# ---------------------------------------------------------------------------

def _wait_for_settle(path: Path, log) -> bool:
    """Block until the file's size has stopped changing. Returns True if settled."""
    started = time.time()
    last_size = -1
    stable_passes = 0
    while time.time() - started < SETTLE_MAX_WAIT_S:
        if not path.exists():
            return False
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size == last_size:
            stable_passes += 1
            if stable_passes >= SETTLE_REQUIRED_PASSES and size > 0:
                return True
        else:
            stable_passes = 0
            last_size = size
        time.sleep(SETTLE_INTERVAL_S)
    log.warning("file did not settle within %ss: %s", SETTLE_MAX_WAIT_S, path.name)
    return False


# ---------------------------------------------------------------------------
# Ingestion handler
# ---------------------------------------------------------------------------

class PDFHandler(FileSystemEventHandler):
    def __init__(self, pusher: TelegramPusher, with_cross_cut: bool, log):
        self.pusher = pusher
        self.with_cross_cut = with_cross_cut
        self.log = log
        self.state = _load_state(SWEEPER_STATE_PATH)

    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if _is_temp_file(path) or path.suffix.lower() != ".pdf":
            return
        self._handle(path)

    def on_moved(self, event):
        # Syncthing finishes a transfer with an atomic rename from temp -> final name.
        # That's the strongest "file is complete" signal we get on this OS, so we
        # treat on_moved as the primary trigger.
        if event.is_directory:
            return
        path = Path(event.dest_path)
        if _is_temp_file(path) or path.suffix.lower() != ".pdf":
            return
        self.log.debug("rename detected (Syncthing/browser finalisation): %s", path.name)
        self._handle(path)

    def _handle(self, path: Path) -> None:
        self.log.info("new file: %s", path.name)
        if not _wait_for_settle(path, self.log):
            self.log.warning("skipping (didn't settle): %s", path.name)
            return

        try:
            h = _hash_file(path)
        except Exception as e:
            self.log.exception("hash failed: %s", path.name)
            self.pusher.send(f"Sweeper: failed to hash {path.name}: {e}")
            return

        if h in self.state.get("processed", {}):
            self.log.info("already processed (sweeper state): %s", path.name)
            return
        if h in _bulk_hashes():
            self.log.info("already processed (bulk state): %s", path.name)
            self.state.setdefault("processed", {})[h] = {
                "file": path.name,
                "skipped_reason": "already in bulk_ingest state",
                "noted_at": datetime.now().isoformat(timespec="seconds"),
            }
            _save_state(self.state, SWEEPER_STATE_PATH)
            return

        MAX_RETRIES = 3
        prior_fail = self.state.get("failed", {}).get(h, {})
        if prior_fail.get("attempts", 0) >= MAX_RETRIES:
            self.log.warning("giving up on %s (%d prior attempts)", path.name, prior_fail.get("attempts", 0))
            self.pusher.send(
                f"Sweeper: giving up on {path.name} — failed {MAX_RETRIES} times. "
                f"Last error: {prior_fail.get('error', '?')[:200]}"
            )
            return

        try:
            self._ingest(path, h)
            # Clear any prior failure record on success
            self.state.get("failed", {}).pop(h, None)
            _save_state(self.state, SWEEPER_STATE_PATH)
        except Exception as e:
            self.log.exception("ingest failed: %s", path.name)
            self.pusher.send(f"Sweeper: ingest failed for {path.name}:\n{e}")
            attempts = int(prior_fail.get("attempts", 0)) + 1
            self.state.setdefault("failed", {})[h] = {
                "file": path.name,
                "error": str(e),
                "trace": traceback.format_exc(),
                "attempts": attempts,
                "first_failed_at": prior_fail.get("first_failed_at") or datetime.now().isoformat(timespec="seconds"),
                "last_failed_at": datetime.now().isoformat(timespec="seconds"),
            }
            _save_state(self.state, SWEEPER_STATE_PATH)

    def _ingest(self, path: Path, h: str) -> None:
        from scripts.bot_pipeline import ingest_and_analyse

        self.pusher.send(f"📥 New file: {path.name}\nTriaging…")
        t0 = time.time()
        result = ingest_and_analyse(path)
        elapsed = time.time() - t0

        triage = result["triage"]
        self.pusher.send(result["triage_summary"])

        # Primary
        self.pusher.send(f"━━━ PRIMARY: {triage['primary_subject']} ━━━")
        self.pusher.send(result["primary_report"])

        # Secondary cross-cuts (if cross_cut enabled)
        if self.with_cross_cut:
            for sec in result["secondaries"]:
                self.pusher.send(f"━━━ {sec['label']} ━━━")
                self.pusher.send(sec.get("report") or f"Failed: {sec.get('error')}")
        elif result["secondaries"]:
            n = len(result["secondaries"])
            self.pusher.send(f"({n} cross-cut(s) skipped — run with --with-cross-cut to enable.)")

        self.pusher.send(f"Done in {elapsed:.0f}s.")

        self.state.setdefault("processed", {})[h] = {
            "file": path.name,
            "primary_type": triage["primary_type"],
            "primary_subject": triage["primary_subject"],
            "processed_at": datetime.now().isoformat(timespec="seconds"),
            "elapsed_seconds": round(elapsed, 1),
        }
        _save_state(self.state, SWEEPER_STATE_PATH)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def sweep(folder: str, with_cross_cut: bool = False, scan_existing: bool = False) -> None:
    """Watch `folder` for new PDFs. Block forever (or until Ctrl-C).

    folder: directory to watch (created if missing)
    with_cross_cut: run cross-cutting analysis per file (slower, more expensive)
    scan_existing: process any PDFs already in the folder at startup
    """
    import logging
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("watchdog").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    log = logging.getLogger("sweeper")

    folder_path = Path(folder).expanduser().resolve()
    folder_path.mkdir(parents=True, exist_ok=True)

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    raw_ids = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    user_ids = [int(x) for x in raw_ids.split(",") if x.strip()]
    pusher = TelegramPusher(token, user_ids, log)

    handler = PDFHandler(pusher, with_cross_cut=with_cross_cut, log=log)

    log.info("watching: %s", folder_path)
    log.info("cross-cut: %s", "ON" if with_cross_cut else "OFF")
    log.info("telegram: %s", "configured" if token and user_ids else "NOT configured")

    if scan_existing:
        log.info("scanning existing PDFs in folder...")
        for pdf in sorted(folder_path.glob("*.pdf")):
            handler._handle(pdf)

    observer = Observer()
    observer.schedule(handler, str(folder_path), recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("stopping (Ctrl-C)...")
        observer.stop()
    observer.join()
    log.info("stopped.")
