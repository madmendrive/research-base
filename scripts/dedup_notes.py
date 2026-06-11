"""Detect and quarantine byte-identical duplicate note PDFs across the data tree.

Duplicates arise when the same source file is re-ingested on different days
(the stored name carries the ingest-date prefix) or routed through different
ingest paths. Each physical copy gets its own KB document/chunks and its own
research-memory rows, double-counting the same estimates and views.

Safety rules:
- Only byte-identical files (SHA256 of file bytes) are ever grouped.
- A group is deduplicated only when every copy resolves to the same canonical
  subject (preferring the subject recorded on its research_sources row over
  the path-inferred one). Copies filed under junk subjects ("News Article")
  or unrecognisable legacy ticker dirs don't block a group. Groups spanning
  two real subjects are reported for review and left untouched — their rows
  carry per-subject granular data.
- Before any copy is removed, the extraction with the most research-memory
  rows in the group is transplanted onto the keeper and re-ingested, so the
  richest granular data always survives.
- Nothing is deleted: loser files move to data/_dedup_quarantine/<ts>/ with
  flattened names (so reindex globs can't resurrect them), and every DB row
  removed is dumped to a JSON manifest in the same folder first.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from scripts import kb
from scripts import research_memory

QUARANTINE_DIRNAME = "_dedup_quarantine"
DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(.+)$")
JUNK_SUBJECTS = {"News Article"}
RESEARCH_CHILD_TABLES = (
    "research_views",
    "research_estimates",
    "research_ratings",
    "research_changes",
    "research_debates",
)


def file_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Store-time prevention
# ---------------------------------------------------------------------------

def existing_identical_copy(dest_dir: Path, src: Path) -> Path | None:
    """Return an already-stored byte-identical copy of src in dest_dir.

    Matches files whose name is src.name under any date prefix (or exactly
    src.name). Filenames routinely contain glob metacharacters ([, ]), so
    candidates are matched by string suffix, never by glob.
    """
    if not dest_dir.is_dir():
        return None
    try:
        src_size = src.stat().st_size
    except OSError:
        return None
    src_hash: str | None = None
    for cand in sorted(dest_dir.iterdir()):
        if not cand.is_file() or cand == src:
            continue
        if cand.name != src.name:
            m = DATE_PREFIX_RE.match(cand.name)
            if not m or m.group(2) != src.name:
                continue
        try:
            if cand.stat().st_size != src_size:
                continue
        except OSError:
            continue
        if src_hash is None:
            src_hash = file_sha256(src)
        if file_sha256(cand) == src_hash:
            return cand
    return None


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def _iter_note_pdfs(data_dir: Path):
    for path in data_dir.glob("**/notes/*.pdf"):
        rel_parts = path.relative_to(data_dir).parts
        if any(part.startswith("_") for part in rel_parts[:-1]):
            continue
        if path.is_file():
            yield path


def _date_prefix(name: str) -> str:
    m = DATE_PREFIX_RE.match(name)
    return m.group(1) if m else "9999-99-99"


def _load_company_tickers() -> set[str]:
    try:
        with open(kb.CONFIG_PATH, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _copy_info(pdf: Path, data_dir: Path, conn: sqlite3.Connection,
               tickers: set[str]) -> dict:
    rel = pdf.relative_to(data_dir).as_posix()
    json_path = pdf.with_name(pdf.name + ".json")
    payload: dict = {}
    if json_path.exists():
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            payload = {}

    try:
        scope = research_memory._infer_scope(pdf, payload)
    except Exception:
        scope = {"corpus_type": "research", "subject_type": "unknown",
                 "subject": pdf.parent.as_posix()}

    source_uri = f"research-structured:{rel}.json"
    src_row = conn.execute(
        "SELECT subject_type, subject FROM research_sources WHERE source_uri = ?",
        (source_uri,),
    ).fetchone()
    rows = 0
    for table in RESEARCH_CHILD_TABLES:
        rows += conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE source_uri = ?", (source_uri,)
        ).fetchone()[0]
    if src_row:
        rows += 1

    doc_row = conn.execute(
        "SELECT id FROM documents WHERE source_path = ?", (str(pdf),)
    ).fetchone()

    parts = rel.split("/")
    subject_type = src_row["subject_type"] if src_row else scope["subject_type"]
    subject = src_row["subject"] if src_row else scope["subject"]
    junk = subject in JUNK_SUBJECTS
    # Canonical location: the directory is a recognised entity home.
    if subject_type == "ticker":
        canonical_dir = parts[0] in tickers
        orphan = not canonical_dir and (
            research_memory.canonicalize_ticker(parts[0]) or parts[0]
        ) not in tickers
    elif subject_type == "author":
        canonical_dir = len(parts) >= 4 and parts[0] in {"Macro", "Semis"} and parts[1] == "authors"
        orphan = False
    elif subject_type == "theme":
        canonical_dir = parts[0] == "Thematic"
        orphan = False
    else:
        canonical_dir = False
        orphan = True

    return {
        "path": str(pdf),
        "rel": rel,
        "date": _date_prefix(pdf.name),
        "subject_type": subject_type,
        "subject": subject,
        "junk": junk,
        "orphan": orphan,
        "canonical_dir": canonical_dir and not junk,
        "rows": rows,
        "has_json": json_path.exists(),
        "json_size": json_path.stat().st_size if json_path.exists() else 0,
        "has_doc": bool(doc_row),
        "source_uri": source_uri,
    }


def _effective_subject(copy: dict) -> tuple[str, str] | None:
    """Subject identity used for group safety; None means non-blocking."""
    if copy["junk"] or copy["orphan"]:
        return None
    subject = research_memory.canonicalize_subject(
        copy["subject_type"], copy["subject"]) or copy["subject"]
    return (copy["subject_type"], subject)


def _keeper_sort_key(copy: dict):
    return (
        copy["canonical_dir"],
        not copy["junk"],
        copy["rows"],
        copy["has_json"],
        copy["has_doc"],
        # earliest ingest date wins; invert by sorting date descending below
    )


def _choose_keeper(copies: list[dict]) -> dict:
    best = sorted(
        copies,
        key=lambda c: (_keeper_sort_key(c), [-ord(ch) for ch in c["date"]], c["rel"]),
        reverse=True,
    )
    return best[0]


def _choose_donor(copies: list[dict]) -> dict:
    return max(copies, key=lambda c: (c["rows"], c["has_json"], c["json_size"]))


def scan_duplicates(data_dir: Path | None = None,
                    conn: sqlite3.Connection | None = None) -> dict:
    """Group byte-identical note PDFs and plan dedup actions (no changes)."""
    data_dir = data_dir or kb.DATA_DIR
    close_conn = conn is None
    conn = conn or kb.connect()
    research_memory.init_schema(conn)
    tickers = _load_company_tickers()
    try:
        by_size: dict[int, list[Path]] = defaultdict(list)
        scanned = 0
        for pdf in _iter_note_pdfs(data_dir):
            scanned += 1
            try:
                by_size[pdf.stat().st_size].append(pdf)
            except OSError:
                continue

        by_hash: dict[str, list[Path]] = defaultdict(list)
        for size, paths in by_size.items():
            if len(paths) < 2:
                continue
            for p in paths:
                by_hash[file_sha256(p)].append(p)

        groups = []
        for digest, paths in sorted(by_hash.items()):
            if len(paths) < 2:
                continue
            copies = [_copy_info(p, data_dir, conn, tickers) for p in sorted(paths)]
            subjects = {s for s in (_effective_subject(c) for c in copies) if s}
            if len(subjects) > 1:
                groups.append({
                    "hash": digest,
                    "action": "skip_multi_subject",
                    "subjects": sorted(f"{t}:{s}" for t, s in subjects),
                    "copies": copies,
                })
                continue
            keeper = _choose_keeper(copies)
            donor = _choose_donor(copies)
            losers = [c for c in copies if c["path"] != keeper["path"]]
            transplant = (
                donor["path"] != keeper["path"]
                and donor["has_json"]
                and (donor["rows"] > keeper["rows"] or not keeper["has_json"])
            )
            groups.append({
                "hash": digest,
                "action": "dedupe",
                "keeper": keeper["path"],
                "donor": donor["path"] if transplant else None,
                "losers": [c["path"] for c in losers],
                "copies": copies,
            })

        dedupe_groups = [g for g in groups if g["action"] == "dedupe"]
        skipped = [g for g in groups if g["action"] != "dedupe"]
        plan = {
            "data_dir": str(data_dir),
            "scanned_pdfs": scanned,
            "duplicate_groups": len(groups),
            "dedupe_groups": len(dedupe_groups),
            "skipped_multi_subject_groups": len(skipped),
            "files_to_quarantine": sum(len(g["losers"]) for g in dedupe_groups),
            "bytes_to_quarantine": sum(
                Path(p).stat().st_size for g in dedupe_groups for p in g["losers"]
                if Path(p).exists()
            ),
            "transplants": sum(1 for g in dedupe_groups if g["donor"]),
            "groups": groups,
        }
        return plan
    finally:
        if close_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def _sidecar_files(pdf: Path) -> list[Path]:
    """The PDF plus every sidecar named after it (.json, _summary.md, ...)."""
    if not pdf.parent.is_dir():
        return []
    return [p for p in sorted(pdf.parent.iterdir())
            if p.is_file() and p.name.startswith(pdf.name)]


def _dump_db_rows(conn: sqlite3.Connection, pdf: Path, source_uri: str) -> dict:
    dump: dict = {"documents": [], "chunks": 0, "research": {}}
    for fpath in _sidecar_files(pdf):
        for doc in conn.execute(
            "SELECT * FROM documents WHERE source_path = ?", (str(fpath),)
        ).fetchall():
            dump["documents"].append(dict(doc))
            dump["chunks"] += conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE document_id = ?", (doc["id"],)
            ).fetchone()[0]
    for table in RESEARCH_CHILD_TABLES + ("research_sources",):
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE source_uri = ?", (source_uri,)
        ).fetchall()
        if rows:
            dump["research"][table] = [dict(r) for r in rows]
    return dump


def _delete_db_rows(conn: sqlite3.Connection, pdf: Path, source_uri: str) -> None:
    for fpath in _sidecar_files(pdf):
        for doc in conn.execute(
            "SELECT id FROM documents WHERE source_path = ?", (str(fpath),)
        ).fetchall():
            conn.execute(
                "DELETE FROM chunks_fts WHERE chunk_id IN "
                "(SELECT id FROM chunks WHERE document_id = ?)", (doc["id"],))
            conn.execute("DELETE FROM chunks WHERE document_id = ?", (doc["id"],))
            conn.execute("DELETE FROM documents WHERE id = ?", (doc["id"],))
    for table in RESEARCH_CHILD_TABLES + ("research_sources",):
        conn.execute(f"DELETE FROM {table} WHERE source_uri = ?", (source_uri,))


def _quarantine_name(rel: str) -> str:
    return rel.replace("/", "__")


def _running_jobs(conn: sqlite3.Connection) -> list[str]:
    try:
        return [f"{r['id']}:{r['kind']}" for r in conn.execute(
            "SELECT id, kind FROM jobs WHERE status = 'running'")]
    except sqlite3.OperationalError:
        return []


def apply_plan(plan: dict, conn: sqlite3.Connection | None = None,
               data_dir: Path | None = None) -> dict:
    """Execute a plan from scan_duplicates: transplant, prune DB, quarantine."""
    data_dir = data_dir or kb.DATA_DIR
    close_conn = conn is None
    conn = conn or kb.connect()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    quarantine = data_dir / QUARANTINE_DIRNAME / stamp
    result = {
        "quarantine": str(quarantine),
        "groups_applied": 0,
        "groups_skipped": 0,
        "files_quarantined": 0,
        "transplants": 0,
        "research_rows_deleted": 0,
        "chunks_deleted": 0,
        "warnings": [],
        "db_dump": {},
    }
    try:
        running = _running_jobs(conn)
        if running:
            raise RuntimeError(
                f"Worker has running job(s) {running}; apply refused. "
                "Wait for the queue to go idle.")

        for group in plan["groups"]:
            if group["action"] != "dedupe":
                result["groups_skipped"] += 1
                continue
            keeper_pdf = Path(group["keeper"])
            losers = [Path(p) for p in group["losers"]]

            # Re-verify byte identity right before acting: the scan may be stale.
            if not keeper_pdf.exists():
                result["warnings"].append(f"keeper missing, group skipped: {keeper_pdf}")
                result["groups_skipped"] += 1
                continue
            keeper_hash = file_sha256(keeper_pdf)
            live_losers = []
            for loser in losers:
                if not loser.exists():
                    result["warnings"].append(f"loser already gone: {loser}")
                elif file_sha256(loser) != keeper_hash:
                    result["warnings"].append(
                        f"content changed since scan, left in place: {loser}")
                else:
                    live_losers.append(loser)
            if not live_losers:
                result["groups_skipped"] += 1
                continue

            copies_by_path = {c["path"]: c for c in group["copies"]}
            keeper_info = copies_by_path[str(keeper_pdf)]
            keeper_json = keeper_pdf.with_name(keeper_pdf.name + ".json")

            # Transplant the richest extraction onto the keeper before pruning.
            if group["donor"]:
                donor_pdf = Path(group["donor"])
                donor_json = donor_pdf.with_name(donor_pdf.name + ".json")
                if donor_json.exists():
                    quarantine.mkdir(parents=True, exist_ok=True)
                    if keeper_json.exists():
                        backup = quarantine / (
                            _quarantine_name(keeper_info["rel"]) + ".json.pre-transplant")
                        shutil.copy2(keeper_json, backup)
                    shutil.copy2(donor_json, keeper_json)
                    donor_summary = donor_pdf.with_name(donor_pdf.name + "_summary.md")
                    keeper_summary = keeper_pdf.with_name(keeper_pdf.name + "_summary.md")
                    if donor_summary.exists() and not keeper_summary.exists():
                        shutil.copy2(donor_summary, keeper_summary)
                    research_memory.ingest_file(conn, keeper_json)
                    result["transplants"] += 1
                else:
                    result["warnings"].append(f"donor json missing: {donor_json}")

            group_rows_before = max(c["rows"] for c in group["copies"])

            for loser in live_losers:
                info = copies_by_path[str(loser)]
                dump = _dump_db_rows(conn, loser, info["source_uri"])
                result["db_dump"][info["rel"]] = dump
                result["chunks_deleted"] += dump["chunks"]
                result["research_rows_deleted"] += sum(
                    len(v) for v in dump["research"].values())
                _delete_db_rows(conn, loser, info["source_uri"])
                quarantine.mkdir(parents=True, exist_ok=True)
                for fpath in _sidecar_files(loser):
                    frel = fpath.relative_to(data_dir).as_posix()
                    shutil.move(str(fpath), str(quarantine / _quarantine_name(frel)))
                    result["files_quarantined"] += 1

            # Post-check: the keeper must retain at least the richest row count
            # seen in the group (transplant target), or its own original rows.
            keeper_rows = 1 if conn.execute(
                "SELECT 1 FROM research_sources WHERE source_uri = ?",
                (keeper_info["source_uri"],)).fetchone() else 0
            for table in RESEARCH_CHILD_TABLES:
                keeper_rows += conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE source_uri = ?",
                    (keeper_info["source_uri"],)).fetchone()[0]
            expected = group_rows_before if group["donor"] else keeper_info["rows"]
            if keeper_rows < expected:
                result["warnings"].append(
                    f"keeper {keeper_info['rel']} retained {keeper_rows} research "
                    f"rows, expected >= {expected} — review db_dump")

            conn.commit()
            result["groups_applied"] += 1

        if result["db_dump"]:
            quarantine.mkdir(parents=True, exist_ok=True)
            dump_path = quarantine / "deleted_db_rows.json"
            dump_path.write_text(
                json.dumps(result["db_dump"], indent=1, ensure_ascii=False, default=str),
                encoding="utf-8")
        return result
    finally:
        if close_conn:
            conn.close()


def run_dedup(apply: bool = False, manifest_path: Path | None = None,
              data_dir: Path | None = None,
              conn: sqlite3.Connection | None = None) -> dict:
    data_dir = data_dir or kb.DATA_DIR
    plan = scan_duplicates(data_dir=data_dir, conn=conn)
    outcome: dict = {"plan": plan, "applied": None}
    if apply:
        outcome["applied"] = apply_plan(plan, conn=conn, data_dir=data_dir)
    if manifest_path is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        mode = "apply" if apply else "dryrun"
        manifest_dir = data_dir / QUARANTINE_DIRNAME
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_dir / f"manifest_{stamp}_{mode}.json"
    manifest_path.write_text(
        json.dumps(outcome, indent=1, ensure_ascii=False, default=str),
        encoding="utf-8")
    outcome["manifest_path"] = str(manifest_path)
    return outcome
