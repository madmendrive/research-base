"""Simple agenda.md scheduler that enqueues ops jobs."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.jobs import enqueue_job

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
STATE_PATH = DATA_DIR / "_kb" / "heartbeat_state.json"

DEFAULT_AGENDA = {
    "timezone": "Asia/Hong_Kong",
    "folder": r"C:\Users\Owner\Downloads\research-inbox",
    "folder_sweep_times": ["08:30", "20:30"],
    "email_sweep_times": ["08:30", "20:30"],
    "headline_sweep_times": ["02:00", "08:00", "14:00", "20:00"],
    "headline_interval_hours": 6,
    "notify": True,
    "folder_analyse": False,
    "email_analyse_attachments": False,
}


def _parse_scalar(value: str):
    value = value.strip()
    if value.lower() in {"true", "yes", "on"}:
        return True
    if value.lower() in {"false", "no", "off"}:
        return False
    if re.fullmatch(r"\d+", value):
        return int(value)
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [x.strip().strip("'\"") for x in inner.split(",")]
    return value.strip("'\"")


def load_agenda(path: str | Path) -> dict:
    agenda = dict(DEFAULT_AGENDA)
    path = Path(path)
    if not path.exists():
        return agenda
    text = path.read_text(encoding="utf-8", errors="replace")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        front = text[3:end] if end != -1 else text[3:]
    else:
        front = text
    current_key = None
    for line in front.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if line.startswith("  - ") and current_key:
            agenda.setdefault(current_key, [])
            if not isinstance(agenda[current_key], list):
                agenda[current_key] = [agenda[current_key]]
            agenda[current_key].append(line[4:].strip().strip("'\""))
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            current_key = key
            if value:
                agenda[key] = _parse_scalar(value)
            else:
                agenda[key] = []
    return agenda


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _time_due(
    now: datetime,
    times: list[str],
    state: dict,
    key: str,
    catch_up: bool = True,
    grace_minutes: int = 10,
) -> bool:
    today = now.strftime("%Y-%m-%d")
    current = now.strftime("%H:%M")
    for scheduled in sorted(str(t) for t in times):
        run_key = f"{key}:{today}:{scheduled}"
        if state.get(run_key):
            continue
        if catch_up:
            if current >= scheduled:
                state[run_key] = now.isoformat(timespec="seconds")
                return True
            continue
        try:
            scheduled_dt = datetime.strptime(f"{today} {scheduled}", "%Y-%m-%d %H:%M")
            scheduled_dt = scheduled_dt.replace(tzinfo=now.tzinfo)
        except ValueError:
            continue
        seconds_after = (now - scheduled_dt).total_seconds()
        if 0 <= seconds_after <= grace_minutes * 60:
            state[run_key] = now.isoformat(timespec="seconds")
            return True
    return False


def _interval_due(now: datetime, hours: int, state: dict, key: str) -> bool:
    last = state.get(key)
    if not last:
        state[key] = now.isoformat(timespec="seconds")
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        state[key] = now.isoformat(timespec="seconds")
        return True
    if (now - last_dt).total_seconds() >= hours * 3600:
        state[key] = now.isoformat(timespec="seconds")
        return True
    return False


def heartbeat(agenda_path: str | Path, run_once: bool = False, sleep_seconds: int = 60) -> None:
    while True:
        agenda = load_agenda(agenda_path)
        tz = ZoneInfo(str(agenda.get("timezone") or "Asia/Hong_Kong"))
        now = datetime.now(tz)
        state = _load_state()
        notify = bool(agenda.get("notify", True))

        folder_times = agenda.get("folder_sweep_times") or []
        if _time_due(now, list(folder_times), state, "folder", catch_up=False):
            enqueue_job(
                "folder_scan",
                {
                    "folder": agenda.get("folder"),
                    "notify": notify,
                    "analyse": bool(agenda.get("folder_analyse", False)),
                },
            )

        email_times = agenda.get("email_sweep_times") or []
        if _time_due(now, list(email_times), state, "email", catch_up=False):
            enqueue_job(
                "email_sweep",
                {
                    "notify": notify,
                    "analyse_attachments": bool(agenda.get("email_analyse_attachments", False)),
                },
            )

        headline_times = agenda.get("headline_sweep_times") or []
        if headline_times:
            if _time_due(now, list(headline_times), state, "headline", catch_up=False):
                enqueue_job(
                    "headline_sweep",
                    {"notify": notify, "window_hours": 6, "max_digest_items": 20},
                )
        else:
            interval = int(agenda.get("headline_interval_hours") or 6)
            if _interval_due(now, interval, state, "headline:last_run"):
                enqueue_job(
                    "headline_sweep",
                    {"notify": notify, "window_hours": interval, "max_digest_items": 20},
                )

        _save_state(state)
        if run_once:
            return
        time.sleep(sleep_seconds)
