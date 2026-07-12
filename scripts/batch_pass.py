"""Bulk view-evolution backfill via the Anthropic Message Batches API.

Runs the same Opus second pass as scripts/second_pass.py, but batched at 50%
of standard token prices. Flow:

    python main.py batch-second-pass --dry-run          # count + cost estimate
    python main.py batch-second-pass --submit           # create the batch
    python main.py batch-second-pass --status           # poll
    python main.py batch-second-pass --apply            # enqueue heavy-lane apply

The apply step runs as a batch_second_pass_apply job in the heavy worker lane
because it writes note JSONs and rebuilds entity summaries (single-writer).
Batch state lives in data/_batch_second_pass_state.json; results are
retrievable from Anthropic for 29 days, so a crashed apply can simply re-run
(apply_second_pass skips notes already stamped second_pass_done).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from scripts import second_pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
STATE_PATH = DATA_DIR / "_batch_second_pass_state.json"

# Opus batch pricing: 50% of $5/$25 per MTok.
_INPUT_USD_PER_TOK = 5.0 / 1_000_000 * 0.5
_OUTPUT_USD_PER_TOK = 25.0 / 1_000_000 * 0.5
_EST_OUTPUT_TOKENS = 1500  # typical view-evolution length


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _client():
    from scripts.research import Anthropic

    return Anthropic(max_retries=3, timeout=600.0)


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {"batches": {}}
    return json.loads(STATE_PATH.read_text(encoding="utf-8", errors="replace"))


def _save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


def scan_pending_notes(since: str = "", limit: int = 0) -> list[dict]:
    """Stored note JSONs still lacking their second pass, oldest first.
    since filters on the stored-date filename prefix (YYYY-MM-DD)."""
    targets = []
    for f in DATA_DIR.glob("*/research/notes/*.json"):
        targets.append((f, "single_name", f.parts[-4], "Macro"))
    for f in DATA_DIR.glob("Macro/authors/*/notes/*.json"):
        targets.append((f, "macro", f.parts[-3], "Macro"))
    for f in DATA_DIR.glob("Semis/authors/*/notes/*.json"):
        targets.append((f, "macro", f.parts[-3], "Semis"))
    for f in DATA_DIR.glob("Thematic/*/notes/*.json"):
        targets.append((f, "thematic", f.parts[-3], "Macro"))

    pending = []
    for f, ptype, subject, category in sorted(targets, key=lambda t: t[0].name):
        if subject == "News Article":
            continue
        if since and (not f.name[:4].isdigit() or f.name[:10] < since):
            continue
        try:
            note = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if second_pass.second_pass_done(note):
            continue
        pending.append({
            "json_path": str(f),
            "primary_type": ptype,
            "subject": subject,
            "category": category,
        })
        if limit and len(pending) >= limit:
            break
    return pending


def _custom_id(json_path: str) -> str:
    return hashlib.sha256(json_path.encode("utf-8", errors="replace")).hexdigest()[:24]


def submit_batch(since: str = "", limit: int = 0, dry_run: bool = False) -> dict:
    """Build prompts for every pending note and submit one message batch."""
    from scripts.research import SYNTHESIS_MODEL

    notes = scan_pending_notes(since=since, limit=limit)
    requests = []
    items: dict[str, dict] = {}
    prompt_chars = 0
    skipped = 0
    for item in notes:
        prep = second_pass.prepare_second_pass(
            item["json_path"], primary_type=item["primary_type"],
            subject=item["subject"], category=item["category"])
        if prep is None:
            skipped += 1
            continue
        cid = _custom_id(item["json_path"])
        prompt_chars += len(prep["prompt"])
        items[cid] = item
        requests.append({
            "custom_id": cid,
            "params": {
                "model": SYNTHESIS_MODEL,
                "max_tokens": 16384,
                "messages": [{"role": "user", "content": prep["prompt"]}],
            },
        })

    est_cost = round(
        (prompt_chars / 4) * _INPUT_USD_PER_TOK
        + len(requests) * _EST_OUTPUT_TOKENS * _OUTPUT_USD_PER_TOK, 2)
    stats = {
        "pending_notes": len(notes),
        "requests": len(requests),
        "already_done": skipped,
        "estimated_cost_usd": est_cost,
        "dry_run": dry_run,
    }
    if dry_run or not requests:
        return stats

    batch = _client().messages.batches.create(requests=requests)
    state = _load_state()
    state.setdefault("batches", {})[batch.id] = {
        "submitted_at": _now(),
        "requests": len(requests),
        "estimated_cost_usd": est_cost,
        "items": items,
    }
    _save_state(state)
    return {**stats, "batch_id": batch.id, "processing_status": batch.processing_status}


def _latest_batch_id(state: dict) -> str | None:
    batches = state.get("batches", {})
    if not batches:
        return None
    return max(batches, key=lambda b: batches[b].get("submitted_at", ""))


def batch_status(batch_id: str | None = None) -> dict:
    state = _load_state()
    batch_id = batch_id or _latest_batch_id(state)
    if not batch_id:
        return {"error": "no submitted batches in state"}
    batch = _client().messages.batches.retrieve(batch_id)
    counts = batch.request_counts
    return {
        "batch_id": batch_id,
        "processing_status": batch.processing_status,
        "succeeded": counts.succeeded,
        "errored": counts.errored,
        "processing": counts.processing,
        "canceled": counts.canceled,
        "expired": counts.expired,
        "applied_at": state["batches"].get(batch_id, {}).get("applied_at"),
    }


def apply_batch(batch_id: str | None = None) -> dict:
    """Merge every succeeded result into its note, then rebuild each touched
    entity's summaries once. Safe to re-run — applied notes are skipped."""
    state = _load_state()
    batch_id = batch_id or _latest_batch_id(state)
    if not batch_id:
        return {"error": "no submitted batches in state"}
    record = state.get("batches", {}).get(batch_id)
    if record is None:
        return {"error": f"batch {batch_id} not in state file"}

    client = _client()
    batch = client.messages.batches.retrieve(batch_id)
    if batch.processing_status != "ended":
        return {"batch_id": batch_id, "status": "not_ready",
                "processing_status": batch.processing_status}

    applied = already = errored = missing = failed_apply = 0
    errors: list[str] = []
    entities: dict[tuple, Path] = {}
    for result in client.messages.batches.results(batch_id):
        item = record["items"].get(result.custom_id)
        if item is None:
            missing += 1
            continue
        if result.result.type != "succeeded":
            errored += 1
            errors.append(f"{Path(item['json_path']).name}: {result.result.type}")
            continue
        msg = result.result.message
        text = next((b.text for b in msg.content if b.type == "text"), "")
        if not text.strip():
            errored += 1
            errors.append(f"{Path(item['json_path']).name}: empty result")
            continue
        try:
            row = second_pass.apply_second_pass(
                item["json_path"], primary_type=item["primary_type"],
                subject=item["subject"], category=item["category"],
                view_evolution=text, rebuild=False)
        except Exception as e:
            failed_apply += 1
            errors.append(f"{Path(item['json_path']).name}: {type(e).__name__}: {e}")
            continue
        if row["status"] == "already_done":
            already += 1
            continue
        applied += 1
        entity_dir = Path(item["json_path"]).parent.parent
        entities[(item["primary_type"], item["subject"], item["category"], str(entity_dir))] = entity_dir

    rebuilt = rebuild_errors = 0
    for (ptype, subject, category, _key), entity_dir in entities.items():
        try:
            second_pass.rebuild_entity_summaries(ptype, subject, category, entity_dir)
            rebuilt += 1
        except Exception as e:
            rebuild_errors += 1
            errors.append(f"rebuild {subject}: {type(e).__name__}: {e}")

    record["applied_at"] = _now()
    record["apply_stats"] = {
        "applied": applied, "already_done": already, "errored": errored,
        "failed_apply": failed_apply, "missing_mapping": missing,
        "entities_rebuilt": rebuilt, "rebuild_errors": rebuild_errors,
    }
    _save_state(state)
    return {
        "batch_id": batch_id, "status": "applied", **record["apply_stats"],
        "errors": errors[:20],
    }
