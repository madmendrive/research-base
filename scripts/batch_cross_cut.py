"""Corpus-wide cross-cut backfill via the Anthropic Message Batches API.

Runs the Opus synthesis half of analyse_research / analyse_thematic for every
pending (document x secondary entity) pair at 50% of standard token prices.

Differences from the realtime path (scripts/bulk_cross_cut.py), by design:
- No per-pair Sonnet extraction. Every stored doc already has an extraction
  JSON next to it; the synthesis prompt also carries the raw document text in
  the system block (same 30K-char cap as the classic path), so the model can
  read the original either way.
- One Opus request per pair, submitted in size-capped batches.

Flow mirrors batch_pass.py:

    python main.py batch-cross-cut --dry-run
    python main.py batch-cross-cut --submit [--limit N]
    python main.py batch-cross-cut --status
    python main.py batch-cross-cut --apply     # heavy-lane job

Apply writes each analysis to the target's analyses/ dir and marks the pair
in _bulk_cross_cut_state.json, so the realtime runner and future dry-runs see
the same done-set. Failed pairs stay unmarked and will be retried by either
path. State in data/_batch_cross_cut_batches.json.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
STATE_PATH = DATA_DIR / "_batch_cross_cut_batches.json"

DOC_TEXT_CAP = 30_000        # chars — matches the classic cached_document_block usage
EXTRACTION_CAP = 20_000      # chars of stored extraction JSON per prompt
TICKER_SUMMARY_CAP = 40_000  # chars of the target ticker's summary JSON
MAX_BATCH_BYTES = 150 * 1024 * 1024  # stay well under the API's 256 MB limit
MAX_BATCH_REQUESTS = 10_000
# Hard spend guard: with all caps applied no request should approach this;
# one exceeding it means a prompt builder regressed — abort, don't upload.
# (Uncapped theme prompts once hit ~1.8MB each and 10x'd the estimated cost.)
MAX_REQUEST_BYTES = 600_000
DRY_RUN_SAMPLE = 24

# Opus batch pricing: 50% of $5/$25 per MTok.
_INPUT_USD_PER_TOK = 5.0 / 1_000_000 * 0.5
_OUTPUT_USD_PER_TOK = 25.0 / 1_000_000 * 0.5
_EST_OUTPUT_TOKENS = 2500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today_prefix() -> str:
    return datetime.now().strftime("%Y-%m-%d")


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


def _doc_text(source_path: str, cache: dict) -> str:
    """Extract (and memoize) the document text, capped like the classic path."""
    if source_path not in cache:
        from scripts.classifier import extract_text

        text, err = extract_text(Path(source_path), max_pages=100, max_chars=DOC_TEXT_CAP)
        cache[source_path] = text or ""
    return cache[source_path]


def _stored_extraction(source_path: str) -> str | None:
    """The doc's stored extraction JSON (written at ingest), truncated."""
    json_path = Path(source_path + ".json")
    if not json_path.exists():
        return None
    try:
        data = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if len(text) > EXTRACTION_CAP:
        text = text[:EXTRACTION_CAP] + "\n[...truncated]"
    return text


def _load_json_if_exists(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None


def _ticker_prompt(target: str, extraction: str | None) -> str | None:
    from scripts.research import ANALYSE_PROMPT, _load_companies

    company = _load_companies().get(target)
    if company is None:
        return None
    prompt = ANALYSE_PROMPT.format(company_name=company["name"], ticker=target)
    if extraction:
        prompt += f"\n\n--- NEW RESEARCH (structured extraction) ---\n{extraction}\n"
    else:
        prompt += "\n\n--- NEW RESEARCH ---\nNo structured extraction available; rely on the document text in the system context.\n"
    from scripts.thematic import capped_json

    summary = _load_json_if_exists(DATA_DIR / target / "research" / "summary.json")
    if summary:
        prompt += f"\n--- EXISTING RESEARCH SUMMARY ---\n{capped_json(summary, TICKER_SUMMARY_CAP)}\n"
    else:
        prompt += "\n--- EXISTING RESEARCH SUMMARY ---\nNo existing research stored for this ticker.\n"
    prompt += "\nThe raw text of the new research is provided between <document> tags in the system context, for additional detail.\n"
    return prompt


def _theme_prompt(target: str, extraction: str | None) -> str | None:
    from scripts.research import _load_companies
    from scripts.thematic import (
        ANALYSE_THEMATIC_PROMPT,
        LINKED_SUMMARY_CAP,
        LINKED_TOTAL_CAP,
        THEME_SUMMARY_CAP,
        _load_theme_config,
        capped_json,
    )

    config = _load_theme_config(target)
    if not config:
        return None
    prompt = ANALYSE_THEMATIC_PROMPT.format(theme_name=config["theme"])
    if extraction:
        prompt += f"\n\n--- NEW THEMATIC RESEARCH (structured extraction) ---\n{extraction}\n"
    else:
        prompt += "\n\n--- NEW THEMATIC RESEARCH ---\nNo structured extraction available; rely on the document text in the system context.\n"
    summary = _load_json_if_exists(DATA_DIR / "Thematic" / target / "theme_summary.json")
    if summary:
        prompt += f"\n--- EXISTING THEMATIC SUMMARY ---\n{capped_json(summary, THEME_SUMMARY_CAP)}\n"
    else:
        prompt += f"\n--- EXISTING THEMATIC SUMMARY ---\nNo existing thematic research stored for {config['theme']}.\n"
    prompt += "\n--- LINKED COMPANY RESEARCH ---\n"
    companies = _load_companies()
    linked_chars = 0
    for lt in config.get("linked_tickers", []):
        ticker = lt["ticker"]
        name = companies.get(ticker, {}).get("name", ticker)
        sn = _load_json_if_exists(DATA_DIR / ticker / "research" / "summary.json")
        if sn and linked_chars < LINKED_TOTAL_CAP:
            snippet = capped_json(sn, LINKED_SUMMARY_CAP)
            linked_chars += len(snippet)
            prompt += f"\n### {ticker} ({name}) — Single-Name Research Summary:\n"
            prompt += snippet + "\n"
        elif sn:
            prompt += f"\n### {ticker} ({name}): summary omitted for prompt budget.\n"
        else:
            prompt += f"\n### {ticker} ({name}): No single-name research stored.\n"
    prompt += "\nThe raw text of the new thematic research is provided between <document> tags in the system context, for additional detail.\n"
    return prompt


def _build_request(pair: dict, text_cache: dict) -> dict | None:
    from scripts.llm_provider import cached_document_block
    from scripts.research import SYNTHESIS_MODEL

    extraction = _stored_extraction(pair["source_path"])
    if pair["kind"] == "ticker":
        prompt = _ticker_prompt(pair["target"], extraction)
    else:
        prompt = _theme_prompt(pair["target"], extraction)
    if prompt is None:
        return None
    doc_text = _doc_text(pair["source_path"], text_cache)
    if not doc_text and not extraction:
        return None  # nothing to analyse from
    return {
        "custom_id": _custom_id(pair),
        "params": {
            "model": SYNTHESIS_MODEL,
            "max_tokens": 16384,
            "system": cached_document_block(doc_text),
            "messages": [{"role": "user", "content": prompt}],
        },
    }


def _custom_id(pair: dict) -> str:
    from scripts.bulk_cross_cut import _pair_key

    key = _pair_key(pair["file_hash"], pair["kind"], pair["target"])
    return hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()[:24]


def pending_pairs(limit: int = 0) -> list[dict]:
    from scripts.bulk_cross_cut import derive_all_pairs

    pairs, _cc_state, _corpus = derive_all_pairs()
    pairs = [p for p in pairs if p["source_exists"]]
    if limit:
        pairs = pairs[:limit]
    return pairs


def submit_batches(limit: int = 0, dry_run: bool = False,
                   max_cost_usd: float = 100.0) -> dict:
    """Build one Opus request per pending pair and submit in size-capped
    batches. Building extracts each doc's text once (local, no API cost) —
    a full-corpus submit takes a while.

    max_cost_usd: hard ceiling — after all requests are built (but before
    anything is uploaded), abort if the measured estimate exceeds it. Raise
    it explicitly for a deliberately large run."""
    pairs = pending_pairs(limit=limit)
    if dry_run:
        # Price from REAL assembled requests, sampled per kind — a flat
        # per-pair constant under-estimated the theme slice 15x once.
        by_kind: dict[str, list[dict]] = {"ticker": [], "theme": []}
        for p in pairs:
            by_kind[p["kind"]].append(p)
        text_cache: dict[str, str] = {}
        est_total = 0.0
        sizes = {}
        for kind, kind_pairs in by_kind.items():
            if not kind_pairs:
                continue
            step = max(1, len(kind_pairs) // DRY_RUN_SAMPLE)
            sample = kind_pairs[::step][:DRY_RUN_SAMPLE]
            sample_bytes = []
            for p in sample:
                try:
                    req = _build_request(p, text_cache)
                except Exception:
                    continue
                if req is not None:
                    sample_bytes.append(len(json.dumps(req, ensure_ascii=False).encode("utf-8")))
            if not sample_bytes:
                continue
            avg = sum(sample_bytes) / len(sample_bytes)
            sizes[kind] = {
                "sampled": len(sample_bytes),
                "avg_request_kb": round(avg / 1024, 1),
                "max_request_kb": round(max(sample_bytes) / 1024, 1),
            }
            est_total += len(kind_pairs) * (
                (avg / 4) * _INPUT_USD_PER_TOK
                + _EST_OUTPUT_TOKENS * _OUTPUT_USD_PER_TOK)
        return {
            "pending_pairs": len(pairs),
            "request_sizes": sizes,
            "estimated_cost_usd": round(est_total, 2),
            "dry_run": True,
        }

    text_cache: dict[str, str] = {}
    chunks: list[list[dict]] = [[]]
    chunk_items: list[dict[str, dict]] = [{}]
    chunk_bytes = 0
    skipped = 0
    prompt_chars = 0
    build_errors: list[str] = []
    seen_ids: set[str] = set()
    for pair in pairs:
        cid = _custom_id(pair)
        if cid in seen_ids:
            # Duplicate pair (the API rejects duplicate custom_ids per batch).
            skipped += 1
            continue
        try:
            request = _build_request(pair, text_cache)
        except Exception as e:
            # One unreadable doc/summary must not kill an hour-long submit.
            skipped += 1
            build_errors.append(
                f"{pair['file']} -> {pair['kind']}/{pair['target']}: {type(e).__name__}: {e}")
            continue
        if request is None:
            skipped += 1
            continue
        seen_ids.add(cid)
        size = len(json.dumps(request, ensure_ascii=False).encode("utf-8"))
        if size > MAX_REQUEST_BYTES:
            raise RuntimeError(
                f"request for {pair['file']} -> {pair['kind']}/{pair['target']} is "
                f"{size / 1024:.0f} KB (> {MAX_REQUEST_BYTES // 1024} KB cap). A prompt "
                "builder is embedding unbounded context — refusing to submit.")
        if chunks[-1] and (chunk_bytes + size > MAX_BATCH_BYTES
                           or len(chunks[-1]) >= MAX_BATCH_REQUESTS):
            chunks.append([])
            chunk_items.append({})
            chunk_bytes = 0
        chunks[-1].append(request)
        chunk_items[-1][request["custom_id"]] = {
            "file_hash": pair["file_hash"], "file": pair["file"],
            "source_path": pair["source_path"], "kind": pair["kind"],
            "target": pair["target"],
        }
        chunk_bytes += size
        prompt_chars += size

    if not chunks[0]:
        return {"pending_pairs": len(pairs), "requests": 0, "skipped": skipped}

    n_requests = sum(len(c) for c in chunks)
    est_cost = round(
        (prompt_chars / 4) * _INPUT_USD_PER_TOK
        + n_requests * _EST_OUTPUT_TOKENS * _OUTPUT_USD_PER_TOK, 2)
    if est_cost > max_cost_usd:
        raise RuntimeError(
            f"Measured estimate ${est_cost} for {n_requests} requests exceeds the "
            f"${max_cost_usd} ceiling — nothing submitted. Re-run with a higher "
            "--max-cost after confirming the spend.")

    client = _client()
    state = _load_state()
    batch_ids = []
    for requests, items in zip(chunks, chunk_items):
        batch = client.messages.batches.create(requests=requests)
        state.setdefault("batches", {})[batch.id] = {
            "submitted_at": _now(),
            "requests": len(requests),
            "items": items,
        }
        _save_state(state)
        batch_ids.append(batch.id)

    return {
        "pending_pairs": len(pairs),
        "requests": n_requests,
        "skipped": skipped,
        "build_errors": build_errors[:20],
        "batches": batch_ids,
        "estimated_cost_usd": est_cost,
    }


def batch_status() -> dict:
    state = _load_state()
    out = []
    client = None
    for batch_id, record in sorted(state.get("batches", {}).items(),
                                   key=lambda kv: kv[1].get("submitted_at", "")):
        if record.get("applied_at"):
            out.append({"batch_id": batch_id, "processing_status": "applied",
                        **record.get("apply_stats", {})})
            continue
        client = client or _client()
        batch = client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        out.append({
            "batch_id": batch_id,
            "processing_status": batch.processing_status,
            "succeeded": counts.succeeded, "errored": counts.errored,
            "processing": counts.processing,
        })
    return {"batches": out} if out else {"error": "no submitted batches in state"}


def _analysis_path(item: dict) -> Path:
    stem = Path(item["source_path"]).stem
    name = f"{_today_prefix()}_{stem}_analysis.md"
    if item["kind"] == "ticker":
        return DATA_DIR / item["target"] / "research" / "analyses" / name
    return DATA_DIR / "Thematic" / item["target"] / "analyses" / name


def apply_batches() -> dict:
    """Write each succeeded analysis and mark the pair done in the shared
    bulk-cross-cut state. Safe to re-run; applies every ended batch."""
    from scripts.bulk_cross_cut import CC_STATE_PATH, _load_state as _cc_load, \
        _pair_key, _save_state as _cc_save

    state = _load_state()
    if not state.get("batches"):
        return {"error": "no submitted batches in state"}

    client = _client()
    cc_state = _cc_load(CC_STATE_PATH)
    applied = errored = skipped_done = not_ready = 0
    errors: list[str] = []
    for batch_id, record in state["batches"].items():
        if record.get("applied_at"):
            continue
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status != "ended":
            not_ready += 1
            continue
        batch_applied_start, batch_errored_start = applied, errored
        for result in client.messages.batches.results(batch_id):
            item = record["items"].get(result.custom_id)
            if item is None:
                continue
            key = _pair_key(item["file_hash"], item["kind"], item["target"])
            if key in cc_state.get("processed_pairs", {}):
                skipped_done += 1
                continue
            if result.result.type != "succeeded":
                errored += 1
                errors.append(f"{item['file']} -> {item['kind']}/{item['target']}: {result.result.type}")
                continue
            msg = result.result.message
            text = next((b.text for b in msg.content if b.type == "text"), "")
            if not text.strip():
                errored += 1
                errors.append(f"{item['file']} -> {item['kind']}/{item['target']}: empty result")
                continue
            path = _analysis_path(item)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            cc_state.setdefault("processed_pairs", {})[key] = {
                "file": item["file"], "kind": item["kind"], "target": item["target"],
                "analysis_path": str(path), "completed_at": _now(), "via": "batch",
            }
            applied += 1
            if applied % 50 == 0:
                _cc_save(cc_state, CC_STATE_PATH)
        record["applied_at"] = _now()
        record["apply_stats"] = {
            "applied": applied - batch_applied_start,
            "errored": errored - batch_errored_start,
        }
        _save_state(state)

    _cc_save(cc_state, CC_STATE_PATH)
    return {
        "applied": applied, "errored": errored, "already_done": skipped_done,
        "batches_not_ready": not_ready, "errors": errors[:20],
    }
