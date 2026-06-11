"""SQLite-backed job queue and single-writer worker."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import kb
from scripts.notify import telegram_send, telegram_send_markdownish_html

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _init_jobs(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            dedupe_key TEXT UNIQUE,
            status TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            available_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            last_error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_status_available ON jobs(status, available_at);
        """
    )
    conn.commit()


def enqueue_job(kind: str, payload: dict | None = None, dedupe_key: str | None = None,
                max_attempts: int = 3, delay_seconds: int = 0) -> int:
    payload = payload or {}
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    dedupe_key = dedupe_key or f"{kind}:{kb.text_hash(payload_json)[:24]}"
    now = _now()
    available = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat(timespec="seconds")
    conn = kb.connect()
    _init_jobs(conn)
    try:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO jobs
                (kind, payload_json, dedupe_key, status, attempts, max_attempts,
                 available_at, created_at, updated_at)
            VALUES (?, ?, ?, 'queued', 0, ?, ?, ?, ?)
            """,
            (kind, payload_json, dedupe_key, max_attempts, available, now, now),
        )
        if cur.rowcount == 0:
            row = conn.execute("SELECT id FROM jobs WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
            job_id = int(row["id"]) if row else 0
        else:
            job_id = int(cur.lastrowid)
        conn.commit()
        return job_id
    finally:
        conn.close()


def job_for_dedupe_key(dedupe_key: str) -> dict | None:
    conn = kb.connect()
    _init_jobs(conn)
    try:
        row = conn.execute(
            """
            SELECT id, kind, status, attempts, max_attempts, created_at, updated_at,
                   started_at, finished_at, last_error
            FROM jobs
            WHERE dedupe_key = ?
            """,
            (dedupe_key,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def queued_summary(limit: int = 20) -> list[dict]:
    conn = kb.connect()
    _init_jobs(conn)
    try:
        rows = conn.execute(
            """
            SELECT id, kind, status, attempts, created_at, updated_at, last_error
            FROM jobs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _claim_next(conn, kinds: list[str] | None = None,
                exclude_kinds: list[str] | None = None):
    _init_jobs(conn)
    where = "status = 'queued' AND available_at <= ?"
    params: list = [_now()]
    if kinds:
        where += f" AND kind IN ({','.join('?' for _ in kinds)})"
        params.extend(kinds)
    if exclude_kinds:
        where += f" AND kind NOT IN ({','.join('?' for _ in exclude_kinds)})"
        params.extend(exclude_kinds)
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        f"""
        SELECT * FROM jobs
        WHERE {where}
        ORDER BY id
        LIMIT 1
        """,
        params,
    ).fetchone()
    if not row:
        conn.commit()
        return None
    conn.execute(
        """
        UPDATE jobs
        SET status = 'running', attempts = attempts + 1, started_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (_now(), _now(), row["id"]),
    )
    conn.commit()
    return dict(row)


def _recover_stale_running(conn) -> int:
    stale_minutes = int(os.environ.get("JOB_STALE_MINUTES", "45") or 45)
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)).isoformat(timespec="seconds")
    now = _now()
    cur = conn.execute(
        """
        UPDATE jobs
        SET status = 'queued',
            available_at = ?,
            updated_at = ?,
            started_at = NULL,
            last_error = COALESCE(last_error, 'Recovered stale running job')
        WHERE status = 'running'
          AND started_at IS NOT NULL
          AND started_at <= ?
          AND attempts < max_attempts
        """,
        (now, now, cutoff),
    )
    conn.commit()
    return int(cur.rowcount or 0)


def _complete(conn, job_id: int) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'succeeded', finished_at = ?, updated_at = ?, last_error = NULL WHERE id = ?",
        (_now(), _now(), job_id),
    )
    conn.commit()


def _fail(conn, job: dict, error: str) -> None:
    attempts = int(job.get("attempts") or 0) + 1
    max_attempts = int(job.get("max_attempts") or 3)
    status = "failed" if attempts >= max_attempts else "queued"
    delay = min(900, 30 * attempts)
    available = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="seconds")
    conn.execute(
        """
        UPDATE jobs
        SET status = ?, last_error = ?, available_at = ?, updated_at = ?,
            finished_at = CASE WHEN ? = 'failed' THEN ? ELSE finished_at END
        WHERE id = ?
        """,
        (status, error, available, _now(), status, _now(), job["id"]),
    )
    conn.commit()


def _run_subprocess(args: list[str]) -> str:
    proc = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "main.py"), *args],
        cwd=str(PROJECT_ROOT),
        text=True,
        capture_output=True,
        timeout=3600,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip()[:4000])
    return (proc.stdout or "").strip()


def _process_job(job: dict) -> str:
    payload = json.loads(job["payload_json"])
    kind = job["kind"]

    if kind == "ingest_file":
        from scripts.analyst import research_readthrough
        from scripts.classifier import extract_text
        from scripts.parallel_ingest import _commit_stage, stage_one

        path = Path(payload["path"])
        staged = stage_one(path, force=bool(payload.get("force", False)))
        stage_path = Path(staged["stage_path"])
        committed = _commit_stage(
            stage_path,
            embed=bool(payload.get("embed", True)),
            force_index=bool(payload.get("force", False)),
        )
        record = json.loads(stage_path.read_text(encoding="utf-8", errors="replace"))
        extraction = record.get("extraction") or {}
        document_text, document_error = extract_text(
            path,
            max_pages=int(payload.get("analysis_max_pages", 80) or 80),
            max_chars=int(payload.get("analysis_max_chars", 65000) or 65000),
        )
        result = {
            "triage": record.get("triage") or {},
            "primary_report": (
                extraction.get("analysis_report")
                or extraction.get("detailed_summary")
                or json.dumps(extraction, ensure_ascii=False)[:6500]
            ),
            "extraction_json": extraction,
            "document_excerpt": document_text or "",
            "document_text_error": document_error,
            "secondaries": [],
            "stored_path": committed.get("stored_path"),
            "structured_memory": committed.get("json_path"),
        }
        if payload.get("notify", True):
            telegram_send_markdownish_html(research_readthrough(result, path.name))
        return f"ingested {path} -> {committed.get('stored_path')}"

    if kind == "analyst_question":
        from scripts.analyst import answer_question

        question = payload["question"]
        try:
            answer = answer_question(question)
        except Exception as e:
            telegram_send(
                f"Analyst question failed: {type(e).__name__}: {e}\n\nQuestion: {question[:300]}"
            )
            return f"analyst question failed: {type(e).__name__}"
        try:
            from scripts.learning import log_interaction

            log_interaction(question, answer, channel="telegram", user_id=payload.get("user_id"))
        except Exception:
            pass
        telegram_send_markdownish_html(f"**Q:** {question}\n\n{answer}")
        return f"answered analyst question ({len(answer)} chars)"

    if kind == "store_override":
        from scripts.bot_pipeline import _store_primary_macro, _store_primary_research, _store_primary_thematic

        path = Path(payload["path"])
        mode = payload["mode"]
        arg = payload["arg"]
        if mode == "research":
            report = _store_primary_research(arg, path)
        elif mode == "macro":
            report = _store_primary_macro(arg, path)
        elif mode == "thematic":
            report = _store_primary_thematic(arg, path)
        else:
            raise ValueError(f"unknown override mode {mode}")
        kb.index_file(path, metadata={"job_id": job["id"], "override": payload})
        try:
            from scripts.research_memory import rebuild as rebuild_research_memory

            rebuild_research_memory(force=False)
        except Exception:
            pass
        if payload.get("notify", True):
            telegram_send(f"Stored override {mode} -> {arg}\n\n{report[:3000]}")
        return f"stored override {mode}:{arg}"

    if kind == "store_note":
        from scripts.ops import store_user_note

        result = store_user_note(payload["target"], payload["text"], author=payload.get("author", "Telegram"))
        if payload.get("notify", True):
            telegram_send(f"Stored note for {payload['target']}\n{result['path']}")
        return f"stored note {payload['target']}"

    if kind == "download_materials":
        ticker = payload["ticker"]
        args = ["download-materials", ticker]
        if payload.get("limit"):
            args.extend(["--limit", str(payload["limit"])])
        if payload.get("since"):
            args.extend(["--since", str(payload["since"])])
        if payload.get("deep"):
            args.append("--deep")
        output = _run_subprocess(args)
        kb.reindex_source("company", force=False)
        if payload.get("notify", True):
            telegram_send(f"Downloaded/indexed materials for {ticker}\n\n{output[-2500:]}")
        return f"downloaded {ticker}"

    if kind == "kb_reindex":
        stats = kb.reindex_source(
            payload.get("source", "all"),
            force=bool(payload.get("force", False)),
            limit=int(payload.get("limit", 0) or 0),
        )
        if payload.get("notify", False):
            telegram_send(f"KB reindex complete\n{json.dumps(stats, indent=2)}")
        return json.dumps(stats)

    if kind == "research_map_reindex":
        from scripts.research_memory import rebuild

        stats = rebuild(
            force=bool(payload.get("force", False)),
            limit=int(payload.get("limit", 0) or 0),
        )
        if payload.get("notify", False):
            telegram_send(f"Research map reindex complete\n{json.dumps(stats, indent=2)}")
        return json.dumps(stats)

    if kind == "folder_scan":
        from scripts.folder_scan import folder_scan

        stats = folder_scan(
            payload.get("folder") or r"C:\Users\Owner\Downloads\research-inbox",
            notify=bool(payload.get("notify", True)),
            analyse=bool(payload.get("analyse", False)),
        )
        return json.dumps(stats)

    if kind == "email_sweep":
        from scripts.email_sweep import email_sweep

        stats = email_sweep(
            notify=bool(payload.get("notify", True)),
            analyse_attachments=bool(payload.get("analyse_attachments", False)),
            extract_research=bool(payload.get("extract_research", True)),
        )
        return json.dumps(stats)

    if kind == "headline_sweep":
        from scripts.headlines import headline_sweep

        stats = headline_sweep(
            notify=bool(payload.get("notify", True)),
            max_digest_items=int(payload.get("max_digest_items", payload.get("max_items", 20)) or 20),
            window_hours=int(payload.get("window_hours", 6) or 6),
        )
        return json.dumps(stats)

    if kind == "analyse_headline":
        from scripts.headlines import analyse_headline

        result = analyse_headline(payload["key"], notify=bool(payload.get("notify", True)))
        return json.dumps(result)

    if kind == "confirm_pending":
        from scripts.ops import confirm_pending

        result = confirm_pending(payload["path"])
        if payload.get("notify", True):
            telegram_send(f"Confirmed pending file: {result['file']}\nStored as: {result['stored_as']}")
        return json.dumps(result)

    if kind == "drop_pending":
        from scripts.ops import drop_pending

        result = drop_pending(payload["path"])
        if payload.get("notify", True):
            telegram_send(f"Dropped pending file: {result['file']}")
        return json.dumps(result)

    raise ValueError(f"unknown job kind {kind}")


def _retry_locked(fn, *args, attempts: int = 6, wait_seconds: int = 5):
    """Run a queue-state write, retrying through transient 'database is
    locked' errors. Another process (CLI kb-reindex, backfills) can hold the
    write lock past our busy timeout; queue bookkeeping must wait it out
    rather than crash the daemon."""
    for attempt in range(attempts):
        try:
            return fn(*args)
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or attempt == attempts - 1:
                raise
            print(f"job queue write locked ({e}); retrying in {wait_seconds}s")
            time.sleep(wait_seconds)


def worker(run_once: bool = False, sleep_seconds: int = 5,
           kinds: list[str] | None = None,
           exclude_kinds: list[str] | None = None) -> None:
    """Job worker. With kinds/exclude_kinds, multiple workers can run in
    parallel lanes — e.g. an interactive lane claiming only analyst_question
    (read-mostly, safe alongside anything) and a heavy lane for everything
    else, which stays strictly serial so jobs that mutate the data tree
    (ingests rebuilding entity summaries) never race each other."""
    if kinds and exclude_kinds:
        raise ValueError("pass kinds or exclude_kinds, not both")
    lane = f" lane={','.join(kinds)}" if kinds else (
        f" lane=all-except-{','.join(exclude_kinds)}" if exclude_kinds else "")
    print(f"worker started{lane}")
    conn = kb.connect()
    _init_jobs(conn)
    try:
        while True:
            try:
                recovered = _recover_stale_running(conn)
                if recovered:
                    print(f"recovered {recovered} stale running job(s)")
                job = _claim_next(conn, kinds=kinds, exclude_kinds=exclude_kinds)
            except sqlite3.OperationalError as e:
                # A poll-time lock (concurrent reindex/backfill process) must
                # not kill the worker — 2026-06-11 it did, and every queued
                # job silently stalled until a manual restart.
                if run_once:
                    raise
                print(f"job queue poll failed ({e}); retrying in {sleep_seconds}s")
                time.sleep(sleep_seconds)
                continue
            if not job:
                if run_once:
                    return
                time.sleep(sleep_seconds)
                continue
            try:
                result = _process_job(job)
                _retry_locked(_complete, conn, int(job["id"]))
                print(f"[job {job['id']}] {job['kind']} ok: {result}")
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                _retry_locked(_fail, conn, job, err)
                print(f"[job {job['id']}] {job['kind']} failed: {err}")
                if run_once:
                    raise
            if run_once:
                return
    finally:
        conn.close()
