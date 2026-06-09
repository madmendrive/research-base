"""Structured research memory over extracted note JSON files.

This is the layer above raw KB search: it turns stored research extraction JSON
into queryable author views, estimates, ratings, thesis changes, and debates.
The source files remain the audit trail; these tables are a rebuildable index.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from scripts import kb
from scripts.tickers import canonicalize_subject, canonicalize_ticker

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
COMPANIES_PATH = PROJECT_ROOT / "config" / "companies.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)


def _load_companies() -> dict:
    try:
        return json.loads(COMPANIES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _currency_for_subject(subject: str | None) -> str | None:
    if not subject:
        return None
    subject = canonicalize_ticker(str(subject).strip()) or str(subject).strip()
    companies = _load_companies()
    market = (companies.get(subject) or {}).get("market")
    if market == "TW" or subject.endswith((" TT", ".TW")):
        return "TWD"
    if market == "KR" or subject.endswith((" KS", ".KS")):
        return "KRW"
    if market == "JP" or subject.endswith((" JT", ".T")) or re.match(r"^\d{3,5}[A-Z]?$", subject):
        return "JPY"
    if market == "HK" or subject.endswith((" HK", ".HK")):
        return "HKD"
    if market == "US":
        return "USD"
    return None


def _normalise_target_price_currency(subject: str | None, currency: str | None) -> str | None:
    inferred = _currency_for_subject(subject)
    if not inferred:
        return currency
    if not currency:
        return inferred
    currency = str(currency).strip().upper()
    # A lot of extracted broker notes default to USD when the ticker suffix clearly
    # identifies the quote currency. Trust the market suffix for target prices.
    if currency == "USD" and inferred != "USD":
        return inferred
    return currency


def _num(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("$", "").replace("%", "").strip()
        cleaned = re.sub(r"^[<>~]+", "", cleaned)
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS research_sources (
            source_uri TEXT PRIMARY KEY,
            corpus_type TEXT NOT NULL,
            subject_type TEXT NOT NULL,
            subject TEXT NOT NULL,
            title TEXT,
            author TEXT,
            publisher TEXT,
            published_at TEXT,
            source_kind TEXT,
            source_path TEXT,
            json_path TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS research_views (
            id INTEGER PRIMARY KEY,
            source_uri TEXT NOT NULL REFERENCES research_sources(source_uri) ON DELETE CASCADE,
            subject_type TEXT NOT NULL,
            subject TEXT NOT NULL,
            author TEXT,
            publisher TEXT,
            theme TEXT NOT NULL,
            category TEXT,
            view_text TEXT NOT NULL,
            sentiment TEXT,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            published_at TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS research_estimates (
            id INTEGER PRIMARY KEY,
            source_uri TEXT NOT NULL REFERENCES research_sources(source_uri) ON DELETE CASCADE,
            subject_type TEXT NOT NULL,
            subject TEXT NOT NULL,
            author TEXT,
            publisher TEXT,
            metric TEXT NOT NULL,
            period TEXT,
            value_text TEXT,
            value_num REAL,
            unit TEXT,
            yoy_growth TEXT,
            source_detail TEXT,
            published_at TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS research_ratings (
            id INTEGER PRIMARY KEY,
            source_uri TEXT NOT NULL REFERENCES research_sources(source_uri) ON DELETE CASCADE,
            subject TEXT NOT NULL,
            author TEXT,
            publisher TEXT,
            rating TEXT,
            target_price REAL,
            target_price_currency TEXT,
            previous_target_price REAL,
            published_at TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS research_changes (
            id INTEGER PRIMARY KEY,
            source_uri TEXT NOT NULL REFERENCES research_sources(source_uri) ON DELETE CASCADE,
            subject_type TEXT NOT NULL,
            subject TEXT NOT NULL,
            author TEXT,
            publisher TEXT,
            change_text TEXT NOT NULL,
            published_at TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS research_debates (
            id INTEGER PRIMARY KEY,
            source_uri TEXT NOT NULL REFERENCES research_sources(source_uri) ON DELETE CASCADE,
            subject_type TEXT NOT NULL,
            subject TEXT NOT NULL,
            author TEXT,
            publisher TEXT,
            debate TEXT NOT NULL,
            bull_case TEXT,
            bear_case TEXT,
            author_lean TEXT,
            published_at TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_research_sources_subject ON research_sources(subject_type, subject);
        CREATE INDEX IF NOT EXISTS idx_research_views_subject ON research_views(subject_type, subject);
        CREATE INDEX IF NOT EXISTS idx_research_views_theme ON research_views(theme);
        CREATE INDEX IF NOT EXISTS idx_research_estimates_subject_metric ON research_estimates(subject, metric, period);
        CREATE INDEX IF NOT EXISTS idx_research_ratings_subject ON research_ratings(subject);
        """
    )
    conn.commit()


def _iter_note_json_files() -> Iterable[Path]:
    if not DATA_DIR.exists():
        return []
    for path in DATA_DIR.glob("**/notes/*.json"):
        if "_kb" in path.parts:
            continue
        if path.name.endswith("_state.json"):
            continue
        yield path


def _source_uri(path: Path) -> str:
    rel = path.resolve().relative_to(DATA_DIR.resolve()).as_posix()
    return f"research-structured:{rel}"


def _infer_source_path(json_path: Path) -> str:
    name = json_path.name
    if name.endswith(".json"):
        candidate = json_path.with_name(name[:-5])
        if candidate.exists():
            return str(candidate)
    return str(json_path)


def _infer_scope(path: Path, payload: dict) -> dict:
    rel = path.resolve().relative_to(DATA_DIR.resolve())
    parts = rel.parts
    meta = payload.get("metadata") or {}
    if len(parts) >= 4 and parts[0] == "Thematic":
        return {"corpus_type": "thematic", "subject_type": "theme", "subject": parts[1]}
    if len(parts) >= 5 and parts[0] in {"Macro", "Semis"} and parts[1] == "authors":
        return {
            "corpus_type": parts[0].lower(),
            "subject_type": "author",
            "subject": parts[2],
        }
    if len(parts) >= 4 and parts[1] == "research":
        return {
            "corpus_type": "single_name",
            "subject_type": "ticker",
            "subject": canonicalize_ticker(parts[0]) or parts[0],
        }
    return {
        "corpus_type": "research",
        "subject_type": "unknown",
        "subject": meta.get("title") or path.stem,
    }


def _meta(payload: dict) -> dict:
    meta = payload.get("metadata") or {}
    return {
        "title": meta.get("title"),
        "author": meta.get("author"),
        "publisher": meta.get("source") or meta.get("firm"),
        "published_at": meta.get("date"),
        "source_kind": meta.get("source_type") or meta.get("publication_type"),
    }


def _insert_source(conn, source_uri: str, path: Path, payload: dict, scope: dict, meta: dict) -> None:
    now = _now()
    existing = conn.execute(
        "SELECT source_uri, created_at FROM research_sources WHERE source_uri = ?",
        (source_uri,),
    ).fetchone()
    for table in (
        "research_views",
        "research_estimates",
        "research_ratings",
        "research_changes",
        "research_debates",
    ):
        conn.execute(f"DELETE FROM {table} WHERE source_uri = ?", (source_uri,))
    conn.execute("DELETE FROM research_sources WHERE source_uri = ?", (source_uri,))
    conn.execute(
        """
        INSERT INTO research_sources
            (source_uri, corpus_type, subject_type, subject, title, author, publisher,
             published_at, source_kind, source_path, json_path, raw_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_uri,
            scope["corpus_type"],
            scope["subject_type"],
            canonicalize_subject(scope["subject_type"], scope["subject"]) or scope["subject"],
            meta.get("title"),
            meta.get("author"),
            meta.get("publisher"),
            meta.get("published_at"),
            meta.get("source_kind"),
            _infer_source_path(path),
            str(path),
            _json(payload),
            existing["created_at"] if existing else now,
            now,
        ),
    )


def _insert_view(conn, source_uri: str, scope: dict, meta: dict, theme: str, view: str,
                 sentiment: str | None = None, category: str | None = None, evidence=None,
                 subject_override: str | None = None, subject_type_override: str | None = None) -> None:
    if not theme or not view:
        return
    subject_type = subject_type_override or scope["subject_type"]
    subject = canonicalize_subject(subject_type, subject_override or scope["subject"]) or (subject_override or scope["subject"])
    conn.execute(
        """
        INSERT INTO research_views
            (source_uri, subject_type, subject, author, publisher, theme, category,
             view_text, sentiment, evidence_json, published_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_uri,
            subject_type,
            subject,
            meta.get("author"),
            meta.get("publisher"),
            str(theme),
            category,
            str(view),
            sentiment,
            _json(evidence),
            meta.get("published_at"),
            _now(),
        ),
    )


def _insert_estimate(conn, source_uri: str, scope: dict, meta: dict, metric: str, period: str | None,
                     value, unit: str | None = None, yoy_growth: str | None = None,
                     source_detail: str | None = None, subject_override: str | None = None,
                     subject_type_override: str | None = None) -> None:
    if not metric or value is None:
        return
    subject_type = subject_type_override or scope["subject_type"]
    subject = canonicalize_subject(subject_type, subject_override or scope["subject"]) or (subject_override or scope["subject"])
    value_text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO research_estimates
            (source_uri, subject_type, subject, author, publisher, metric, period,
             value_text, value_num, unit, yoy_growth, source_detail, published_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_uri,
            subject_type,
            subject,
            meta.get("author"),
            meta.get("publisher"),
            str(metric),
            period,
            value_text,
            _num(value),
            unit,
            yoy_growth,
            source_detail,
            meta.get("published_at"),
            _now(),
        ),
    )


def _insert_rating(conn, source_uri: str, scope: dict, meta: dict, rating: dict) -> None:
    if not isinstance(rating, dict) or not any(rating.get(k) for k in ("rating", "target_price")):
        return
    subject = canonicalize_subject(scope["subject_type"], scope["subject"]) or scope["subject"]
    conn.execute(
        """
        INSERT INTO research_ratings
            (source_uri, subject, author, publisher, rating, target_price,
             target_price_currency, previous_target_price, published_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_uri,
            subject,
            meta.get("author"),
            meta.get("publisher"),
            rating.get("rating"),
            _num(rating.get("target_price")),
            _normalise_target_price_currency(subject, rating.get("target_price_currency")),
            _num(rating.get("previous_target_price")),
            meta.get("published_at"),
            _now(),
        ),
    )


def _insert_change(conn, source_uri: str, scope: dict, meta: dict, change: str) -> None:
    if not change:
        return
    subject = canonicalize_subject(scope["subject_type"], scope["subject"]) or scope["subject"]
    conn.execute(
        """
        INSERT INTO research_changes
            (source_uri, subject_type, subject, author, publisher, change_text,
             published_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_uri,
            scope["subject_type"],
            subject,
            meta.get("author"),
            meta.get("publisher"),
            str(change),
            meta.get("published_at"),
            _now(),
        ),
    )


def _insert_debate(conn, source_uri: str, scope: dict, meta: dict, debate: dict) -> None:
    if not isinstance(debate, dict) or not debate.get("debate"):
        return
    subject = canonicalize_subject(scope["subject_type"], scope["subject"]) or scope["subject"]
    conn.execute(
        """
        INSERT INTO research_debates
            (source_uri, subject_type, subject, author, publisher, debate, bull_case,
             bear_case, author_lean, published_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_uri,
            scope["subject_type"],
            subject,
            meta.get("author"),
            meta.get("publisher"),
            debate.get("debate"),
            debate.get("bull_case"),
            debate.get("bear_case"),
            debate.get("author_lean"),
            meta.get("published_at"),
            _now(),
        ),
    )


def _ingest_standard_estimates(conn, source_uri: str, scope: dict, meta: dict, estimates: dict) -> None:
    for metric, by_period in (estimates or {}).items():
        if metric == "other_key_metrics":
            continue
        if not isinstance(by_period, dict):
            continue
        for period, value in by_period.items():
            if isinstance(value, dict):
                _insert_estimate(
                    conn,
                    source_uri,
                    scope,
                    meta,
                    metric,
                    period,
                    value.get("value"),
                    unit=value.get("unit"),
                    yoy_growth=value.get("yoy_growth"),
                    source_detail=value.get("source_detail"),
                )
            else:
                _insert_estimate(conn, source_uri, scope, meta, metric, period, value)
    for entry in (estimates or {}).get("other_key_metrics", []) or []:
        if not isinstance(entry, dict):
            continue
        metric = entry.get("metric")
        values = entry.get("values") or {}
        for period, value in values.items():
            if isinstance(value, dict):
                _insert_estimate(
                    conn,
                    source_uri,
                    scope,
                    meta,
                    metric,
                    period,
                    value.get("value"),
                    unit=value.get("unit"),
                    yoy_growth=value.get("yoy_growth"),
                    source_detail=value.get("source_detail") or value.get("context"),
                )
            else:
                _insert_estimate(conn, source_uri, scope, meta, metric, period, value)


def _ingest_key_data_points(conn, source_uri: str, scope: dict, meta: dict, payload: dict) -> None:
    for entry in payload.get("key_data_points", []) or []:
        if not isinstance(entry, dict):
            continue
        _insert_estimate(
            conn,
            source_uri,
            scope,
            meta,
            entry.get("data_point") or "data_point",
            None,
            entry.get("value"),
            source_detail=entry.get("context"),
        )


def _ingest_single_name(conn, source_uri: str, scope: dict, meta: dict, payload: dict) -> None:
    _ingest_standard_estimates(conn, source_uri, scope, meta, payload.get("key_estimates") or {})
    for entry in payload.get("key_themes", []) or []:
        if not isinstance(entry, dict):
            continue
        _insert_view(
            conn,
            source_uri,
            scope,
            meta,
            entry.get("theme"),
            entry.get("view"),
            sentiment=entry.get("sentiment"),
            evidence=entry,
        )
    _insert_rating(conn, source_uri, scope, meta, payload.get("rating_and_target") or {})
    for change in payload.get("notable_changes", []) or []:
        _insert_change(conn, source_uri, scope, meta, change)
    _ingest_key_data_points(conn, source_uri, scope, meta, payload)


def _ingest_macro_like(conn, source_uri: str, scope: dict, meta: dict, payload: dict) -> None:
    for topic, entry in (payload.get("macro_views") or {}).items():
        if isinstance(entry, dict) and entry.get("view"):
            _insert_view(
                conn,
                source_uri,
                scope,
                meta,
                topic,
                entry.get("view"),
                sentiment=entry.get("sentiment"),
                category=topic,
                evidence=entry,
            )
            for field, value in entry.items():
                if field in {"view", "sentiment", "regions", "key_pairs", "sector_preferences"}:
                    continue
                _insert_estimate(conn, source_uri, scope, meta, f"{topic}.{field}", None, value)
        elif entry:
            _insert_view(conn, source_uri, scope, meta, topic, str(entry), category=topic)

    for entry in payload.get("themes", []) or []:
        if not isinstance(entry, dict):
            continue
        _insert_view(
            conn,
            source_uri,
            scope,
            meta,
            entry.get("theme"),
            entry.get("detailed_view") or entry.get("view"),
            category=entry.get("category"),
            evidence={"trades_or_implications": entry.get("trades_or_implications")},
        )

    for entry in payload.get("recommended_trades_or_positioning", []) or []:
        if isinstance(entry, dict):
            _insert_view(
                conn,
                source_uri,
                scope,
                meta,
                f"positioning: {entry.get('trade')}",
                entry.get("rationale"),
                category="positioning",
                evidence=entry,
            )
    _ingest_key_data_points(conn, source_uri, scope, meta, payload)


def _ingest_thematic(conn, source_uri: str, scope: dict, meta: dict, payload: dict) -> None:
    for metric, by_period in (payload.get("theme_estimates") or {}).items():
        if not isinstance(by_period, dict):
            continue
        for period, value in by_period.items():
            if isinstance(value, dict):
                _insert_estimate(
                    conn,
                    source_uri,
                    scope,
                    meta,
                    metric,
                    period,
                    value.get("value"),
                    source_detail=value.get("source_detail"),
                )
            else:
                _insert_estimate(conn, source_uri, scope, meta, metric, period, value)

    for ticker, mention in (payload.get("company_specific_mentions") or {}).items():
        if not isinstance(mention, dict):
            continue
        canonical_ticker = canonicalize_ticker(ticker) or ticker
        view = "; ".join(str(x) for x in mention.get("key_points", []) or mention.get("mentions", []) or [])
        _insert_view(
            conn,
            source_uri,
            scope,
            meta,
            f"company mention: {canonical_ticker}",
            view or mention.get("outlook"),
            sentiment=mention.get("outlook"),
            subject_override=canonical_ticker,
            subject_type_override="ticker",
            evidence=mention,
        )
        for metric, value in (mention.get("implied_estimates") or {}).items():
            if isinstance(value, dict):
                for period, period_value in value.items():
                    _insert_estimate(
                        conn,
                        source_uri,
                        scope,
                        meta,
                        metric,
                        period,
                        period_value,
                        subject_override=canonical_ticker,
                        subject_type_override="ticker",
                    )
            else:
                _insert_estimate(
                    conn,
                    source_uri,
                    scope,
                    meta,
                    metric,
                    None,
                    value,
                    subject_override=canonical_ticker,
                    subject_type_override="ticker",
                )

    for entry in payload.get("themes", []) or []:
        if isinstance(entry, dict):
            _insert_view(
                conn,
                source_uri,
                scope,
                meta,
                entry.get("theme"),
                entry.get("detailed_view") or entry.get("view"),
                category=entry.get("category"),
                evidence={"trades_or_implications": entry.get("trades_or_implications")},
            )
    for entry in payload.get("key_debates", []) or []:
        _insert_debate(conn, source_uri, scope, meta, entry)
    _ingest_key_data_points(conn, source_uri, scope, meta, payload)


def ingest_file(conn: sqlite3.Connection, path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    source_uri = _source_uri(path)
    scope = _infer_scope(path, payload)
    meta = _meta(payload)
    init_schema(conn)
    _insert_source(conn, source_uri, path, payload, scope, meta)
    if scope["corpus_type"] == "single_name":
        _ingest_single_name(conn, source_uri, scope, meta, payload)
    elif scope["corpus_type"] == "thematic":
        _ingest_thematic(conn, source_uri, scope, meta, payload)
    else:
        _ingest_macro_like(conn, source_uri, scope, meta, payload)
    return {"source_uri": source_uri, **scope}


def rebuild(force: bool = False, limit: int = 0) -> dict:
    conn = kb.connect()
    init_schema(conn)
    if force:
        conn.executescript(
            """
            DELETE FROM research_debates;
            DELETE FROM research_changes;
            DELETE FROM research_ratings;
            DELETE FROM research_estimates;
            DELETE FROM research_views;
            DELETE FROM research_sources;
            """
        )
        conn.commit()
    stats = {"scanned": 0, "indexed": 0, "errors": []}
    try:
        for path in _iter_note_json_files():
            if limit and stats["scanned"] >= limit:
                break
            stats["scanned"] += 1
            try:
                ingest_file(conn, path)
                stats["indexed"] += 1
            except Exception as e:
                stats["errors"].append({"path": str(path), "error": f"{type(e).__name__}: {e}"})
        conn.commit()
        stats.update(status(conn))
        return stats
    finally:
        conn.close()


def _research_pdf_paths() -> list[Path]:
    patterns = [
        "*/*research*/notes/*.pdf",
        "Macro/authors/*/notes/*.pdf",
        "Semis/authors/*/notes/*.pdf",
        "Thematic/*/notes/*.pdf",
    ]
    out = []
    for pattern in patterns:
        out.extend(DATA_DIR.glob(pattern))
    return sorted(set(p for p in out if p.is_file()))


def extraction_backlog(limit: int = 20) -> list[dict]:
    rows = []
    for pdf in _research_pdf_paths():
        if not pdf.with_name(pdf.name + ".json").exists():
            rows.append({"path": str(pdf), "size": pdf.stat().st_size})
            if limit and len(rows) >= limit:
                break
    return rows


def status(conn: sqlite3.Connection | None = None) -> dict:
    close_conn = conn is None
    conn = conn or kb.connect()
    init_schema(conn)
    try:
        counts = {}
        for table in (
            "research_sources",
            "research_views",
            "research_estimates",
            "research_ratings",
            "research_changes",
            "research_debates",
        ):
            counts[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        by_corpus = [
            dict(r)
            for r in conn.execute(
                "SELECT corpus_type, count(*) AS n FROM research_sources GROUP BY corpus_type ORDER BY n DESC"
            )
        ]
        by_subject = [
            dict(r)
            for r in conn.execute(
                """
                SELECT subject_type, subject, count(*) AS n
                FROM research_sources
                GROUP BY subject_type, subject
                ORDER BY n DESC, subject
                LIMIT 20
                """
            )
        ]
        pdfs = _research_pdf_paths()
        missing = [p for p in pdfs if not p.with_name(p.name + ".json").exists()]
        return {
            **counts,
            "by_corpus": by_corpus,
            "top_subjects": by_subject,
            "research_pdfs": len(pdfs),
            "pdfs_missing_extraction_json": len(missing),
        }
    finally:
        if close_conn:
            conn.close()


def subject_snapshot(subject: str, limit: int = 8) -> str:
    conn = kb.connect()
    init_schema(conn)
    canonical_subject = canonicalize_ticker(subject) or subject
    subject_l = canonical_subject.lower()
    try:
        sources = [
            dict(r)
            for r in conn.execute(
                """
                SELECT * FROM research_sources
                WHERE lower(subject) = ? OR lower(subject) LIKE ?
                ORDER BY published_at DESC, updated_at DESC
                LIMIT ?
                """,
                (subject_l, f"%{subject_l}%", limit),
            )
        ]
        views = [
            dict(r)
            for r in conn.execute(
                """
                SELECT publisher, author, theme, sentiment, view_text, published_at
                FROM research_views
                WHERE lower(subject) = ? OR lower(theme) LIKE ?
                ORDER BY published_at DESC, id DESC
                LIMIT ?
                """,
                (subject_l, f"%{subject_l}%", limit),
            )
        ]
        estimates = [
            dict(r)
            for r in conn.execute(
                """
                SELECT publisher, author, metric, period, value_text, unit, yoy_growth, published_at
                FROM research_estimates
                WHERE lower(subject) = ? OR lower(metric) LIKE ?
                ORDER BY
                    CASE lower(metric)
                        WHEN 'revenue' THEN 1
                        WHEN 'eps' THEN 2
                        WHEN 'gross_margin' THEN 3
                        WHEN 'operating_margin' THEN 4
                        WHEN 'capex' THEN 5
                        ELSE 20
                    END,
                    period,
                    published_at DESC,
                    id DESC
                LIMIT ?
                """,
                (subject_l, f"%{subject_l}%", limit),
            )
        ]
        ratings = [
            dict(r)
            for r in conn.execute(
                """
                SELECT publisher, author, rating, target_price, target_price_currency,
                       previous_target_price, published_at
                FROM research_ratings
                WHERE lower(subject) = ?
                ORDER BY published_at DESC, id DESC
                LIMIT ?
                """,
                (subject_l, limit),
            )
        ]
    finally:
        conn.close()

    lines = [f"# Research Memory Snapshot: {canonical_subject}", ""]
    lines.append(f"Sources indexed: {len(sources)}")
    if ratings:
        lines.append("\n## Ratings / Targets")
        for r in ratings:
            tp = r.get("target_price")
            prev = r.get("previous_target_price")
            lines.append(
                f"- {r.get('publisher') or 'Unknown'} ({r.get('published_at') or 'n.d.'}): "
                f"{r.get('rating') or 'n/a'}, TP {tp or 'n/a'} {r.get('target_price_currency') or ''}"
                + (f" (prev {prev})" if prev else "")
            )
    if estimates:
        lines.append("\n## Estimates / Data Points")
        for e in estimates:
            lines.append(
                f"- {e.get('publisher') or 'Unknown'} {e.get('metric')} {e.get('period') or ''}: "
                f"{e.get('value_text')} {e.get('unit') or ''}"
                + (f" ({e.get('yoy_growth')})" if e.get("yoy_growth") else "")
            )
    if views:
        lines.append("\n## Views")
        for v in views:
            view = (v.get("view_text") or "").replace("\n", " ")
            if len(view) > 360:
                view = view[:357] + "..."
            lines.append(
                f"- {v.get('publisher') or v.get('author') or 'Unknown'} | "
                f"{v.get('theme')} [{v.get('sentiment') or 'n/a'}]: {view}"
            )
    return "\n".join(lines).strip() + "\n"


def query_context(query: str, limit: int = 10) -> str:
    """Return compact structured-memory context for analyst prompts."""
    query = (query or "").strip()
    if not query:
        return ""
    stopwords = {
        "what", "which", "when", "where", "how", "the", "for", "and", "or",
        "are", "is", "was", "were", "did", "you", "your", "latest", "view",
        "views", "estimate", "estimates", "similar", "different", "from",
    }
    source_aliases = {
        "JPM": ["jpm", "jp morgan", "j.p. morgan", "jpmorgan", "jpmorgan chase"],
        "SemiAnalysis": ["semianalysis", "semi analysis"],
        "Morgan Stanley": ["morgan stanley"],
        "Goldman Sachs": ["goldman", "goldman sachs"],
        "BofA": ["bofa", "bank of america"],
        "Barclays": ["barclays"],
        "UBS": ["ubs"],
        "Citi": ["citi", "citigroup"],
        "Nomura": ["nomura"],
        "Jefferies": ["jefferies"],
    }
    query_l = query.lower()
    requested_sources = [
        (canonical, aliases)
        for canonical, aliases in source_aliases.items()
        if any(alias in query_l for alias in aliases)
    ]
    source_alias_words = {
        part
        for _canonical, aliases in requested_sources
        for alias in aliases
        for part in re.findall(r"[a-z0-9]+", alias)
    }

    terms = []

    def add_term(term: str) -> None:
        term_l = term.lower().strip("._-$")
        if not term_l or term_l in stopwords or term_l in source_alias_words:
            return
        if term_l not in terms:
            terms.append(term_l)
        canonical = canonicalize_ticker(term)
        if canonical and canonical != term:
            canonical_l = canonical.lower()
            if canonical_l not in terms:
                terms.append(canonical_l)
            for part in re.findall(r"[A-Za-z0-9]{2,}", canonical):
                part_l = part.lower()
                if part_l not in terms and part_l not in stopwords:
                    terms.append(part_l)

    for term in re.findall(r"[A-Za-z0-9.$_-]{2,}", query):
        add_term(term)
        if len(terms) >= 18:
            terms = terms[:18]
            break
    if not terms and not requested_sources:
        return ""

    def like_clauses(cols: list[str], search_terms: list[str] | None = None) -> tuple[str, list[str]]:
        search_terms = terms if search_terms is None else search_terms
        clauses = []
        params = []
        for term in search_terms:
            for col in cols:
                clauses.append(f"lower({col}) LIKE ?")
                params.append(f"%{term}%")
        return " OR ".join(clauses) if clauses else "1=1", params

    def relevance_expr(cols: list[str], search_terms: list[str] | None = None) -> tuple[str, list[str]]:
        search_terms = terms if search_terms is None else search_terms
        parts = []
        params = []
        for term in search_terms:
            term_l = term.lower()
            weight = 3 if term_l in {"asp", "dram", "nand", "hbm", "qoq", "yoy"} else 1
            for col in cols:
                parts.append(f"CASE WHEN lower({col}) LIKE ? THEN {weight} ELSE 0 END")
                params.append(f"%{term}%")
        return " + ".join(parts) if parts else "0", params

    source_terms = [
        alias
        for _canonical, aliases in requested_sources
        for alias in aliases
    ]

    conn = kb.connect()
    init_schema(conn)
    try:
        source_context_lines = []
        source_where = ""
        source_params = []
        if requested_sources:
            source_where, source_params = like_clauses(
                ["s.publisher", "s.author", "s.title", "s.source_path"],
                source_terms,
            )
            for canonical, aliases in requested_sources:
                alias_where, alias_params = like_clauses(
                    ["publisher", "author", "title", "source_path"],
                    aliases,
                )
                rows = [
                    dict(r)
                    for r in conn.execute(
                        f"""
                        SELECT title, publisher, author, published_at, corpus_type, subject, source_path
                        FROM research_sources
                        WHERE {alias_where}
                        ORDER BY published_at DESC, updated_at DESC
                        LIMIT 5
                        """,
                        alias_params,
                    )
                ]
                if rows:
                    source_context_lines.append(f"- {canonical}: {len(rows)} recent structured source match(es) sampled.")
                    for row in rows[:3]:
                        source_context_lines.append(
                            f"  - {row.get('published_at') or 'n.d.'}: "
                            f"{row.get('title') or Path(row.get('source_path') or '').name} "
                            f"({row.get('publisher') or row.get('author') or 'Unknown'})."
                        )
                else:
                    source_context_lines.append(
                        f"- {canonical}: no structured research source currently matches this publisher/author name."
                    )

            if all("no structured research source" in line for line in source_context_lines):
                lines = ["<structured_research_memory>", "Source coverage:", *source_context_lines]
                lines.append("</structured_research_memory>")
                return "\n".join(lines)

        where, params = like_clauses([
            "v.subject", "v.theme", "v.view_text", "v.publisher", "v.author",
            "s.title",
        ])
        if source_where:
            where = f"({where}) AND ({source_where})"
            params = [*params, *source_params]
        views = [
            dict(r)
            for r in conn.execute(
                f"""
                SELECT v.subject, v.publisher, v.author, v.theme, v.sentiment,
                       v.view_text, v.published_at, s.title
                FROM research_views v
                JOIN research_sources s ON s.source_uri = v.source_uri
                WHERE {where}
                ORDER BY v.published_at DESC, v.id DESC
                LIMIT ?
                """,
                [*params, limit],
            )
        ]

        where, params = like_clauses([
            "e.subject", "e.metric", "e.period", "e.value_text", "e.yoy_growth",
            "e.source_detail", "e.publisher", "e.author", "s.title",
        ])
        estimate_relevance, estimate_relevance_params = relevance_expr([
            "e.subject", "e.metric", "e.period", "e.value_text", "e.yoy_growth",
            "e.source_detail", "s.title",
        ])
        if source_where:
            where = f"({where}) AND ({source_where})"
            params = [*params, *source_params]
        estimates = [
            dict(r)
            for r in conn.execute(
                f"""
                SELECT e.subject, e.publisher, e.author, e.metric, e.period,
                       e.value_text, e.unit, e.yoy_growth, e.source_detail,
                       e.published_at, s.title, ({estimate_relevance}) AS relevance
                FROM research_estimates e
                JOIN research_sources s ON s.source_uri = e.source_uri
                WHERE {where}
                ORDER BY relevance DESC, e.published_at DESC, e.id DESC
                LIMIT ?
                """,
                [*estimate_relevance_params, *params, limit],
            )
        ]

        where, params = like_clauses(["r.subject", "r.publisher", "r.author", "s.title"])
        if source_where:
            where = f"({where}) AND ({source_where})"
            params = [*params, *source_params]
        ratings = [
            dict(r)
            for r in conn.execute(
                f"""
                SELECT r.subject, r.publisher, r.author, r.rating, r.target_price,
                       r.target_price_currency, r.previous_target_price,
                       r.published_at, s.title
                FROM research_ratings r
                JOIN research_sources s ON s.source_uri = r.source_uri
                WHERE {where}
                ORDER BY r.published_at DESC, r.id DESC
                LIMIT ?
                """,
                [*params, max(4, limit // 2)],
            )
        ]

        where, params = like_clauses([
            "d.subject", "d.debate", "d.bull_case", "d.bear_case",
            "d.publisher", "d.author", "s.title",
        ])
        if source_where:
            where = f"({where}) AND ({source_where})"
            params = [*params, *source_params]
        debates = [
            dict(r)
            for r in conn.execute(
                f"""
                SELECT d.subject, d.publisher, d.author, d.debate, d.bull_case,
                       d.bear_case, d.author_lean, d.published_at, s.title
                FROM research_debates d
                JOIN research_sources s ON s.source_uri = d.source_uri
                WHERE {where}
                ORDER BY d.published_at DESC, d.id DESC
                LIMIT ?
                """,
                [*params, max(4, limit // 2)],
            )
        ]
    finally:
        conn.close()

    if not any((views, estimates, ratings, debates, locals().get("source_context_lines"))):
        return ""

    lines = ["<structured_research_memory>"]
    if locals().get("source_context_lines"):
        lines.append("Source coverage:")
        lines.extend(source_context_lines)
    if ratings:
        lines.append("Ratings/targets:")
        for r in ratings:
            lines.append(
                f"- {r.get('subject')} | {r.get('publisher') or r.get('author') or 'Unknown'} "
                f"({r.get('published_at') or 'n.d.'}): {r.get('rating') or 'n/a'}, "
                f"TP {r.get('target_price') or 'n/a'} {r.get('target_price_currency') or ''}, "
                f"prev {r.get('previous_target_price') or 'n/a'}"
            )
    if estimates:
        lines.append("Estimates/data points:")
        for e in estimates:
            detail = f"; {e.get('source_detail')}" if e.get("source_detail") else ""
            growth = f"; {e.get('yoy_growth')}" if e.get("yoy_growth") else ""
            lines.append(
                f"- {e.get('subject')} | {e.get('publisher') or e.get('author') or 'Unknown'} "
                f"({e.get('published_at') or 'n.d.'}; {e.get('title') or 'untitled'}) "
                f"{e.get('metric')} {e.get('period') or ''}: {e.get('value_text')} "
                f"{e.get('unit') or ''}{growth}{detail}"
            )
    if views:
        lines.append("Views/assumptions:")
        for v in views:
            view = (v.get("view_text") or "").replace("\n", " ")
            if len(view) > 520:
                view = view[:517] + "..."
            lines.append(
                f"- {v.get('subject')} | {v.get('publisher') or v.get('author') or 'Unknown'} "
                f"({v.get('published_at') or 'n.d.'}; {v.get('title') or 'untitled'}) | {v.get('theme')} "
                f"[{v.get('sentiment') or 'n/a'}]: {view}"
            )
    if debates:
        lines.append("Debates:")
        for d in debates:
            lines.append(
                f"- {d.get('subject')} | {d.get('publisher') or d.get('author') or 'Unknown'} "
                f"{d.get('debate')} | bull: {d.get('bull_case') or 'n/a'} | "
                f"bear: {d.get('bear_case') or 'n/a'} | lean: {d.get('author_lean') or 'n/a'}"
            )
    lines.append("</structured_research_memory>")
    return "\n".join(lines)
