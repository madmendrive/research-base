"""Knowledge-base index over the existing research-pipeline data tree.

The file tree remains the audit source of truth. This module adds a durable
SQLite index for metadata, chunks, FTS search, and cached embeddings.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
KB_DIR = DATA_DIR / "_kb"
DB_PATH = KB_DIR / "kb.sqlite"
CONFIG_PATH = PROJECT_ROOT / "config" / "companies.json"

SUPPORTED_SUFFIXES = {".pdf", ".md", ".txt", ".html", ".htm", ".jsonl", ".eml"}
DEFAULT_CHUNK_CHARS = 3500
DEFAULT_CHUNK_OVERLAP = 400
DEFAULT_SEARCH_LIMIT = 8


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(data) -> str:
    return json.dumps(data or {}, ensure_ascii=False, sort_keys=True)


def _loads(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def slugify(value: str, max_len: int = 90) -> str:
    value = re.sub(r"[^\w\s.-]+", "", value or "", flags=re.UNICODE).strip()
    value = re.sub(r"\s+", "_", value)
    return (value[:max_len] or "untitled").strip("._")


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY,
            source_uri TEXT NOT NULL UNIQUE,
            source_type TEXT NOT NULL,
            title TEXT,
            source_path TEXT,
            url TEXT,
            author TEXT,
            publisher TEXT,
            hash TEXT,
            entities_json TEXT NOT NULL DEFAULT '{}',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            text TEXT NOT NULL,
            token_estimate INTEGER NOT NULL DEFAULT 0,
            embedding_model TEXT,
            embedding_json TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(document_id, chunk_index)
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            chunk_id UNINDEXED,
            text,
            title,
            source_type,
            entity_text
        );

        CREATE INDEX IF NOT EXISTS idx_documents_source_type ON documents(source_type);
        CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(hash);
        CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);
        """
    )
    conn.commit()


def file_hash(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def _strip_html(text: str) -> str:
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return soup.get_text("\n")
    except Exception:
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        return html.unescape(text)


def extract_text_from_file(path: str | Path, max_chars: int | None = None) -> str:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from scripts.classifier import extract_text

        text, err = extract_text(path, max_pages=300, max_chars=max_chars)
        if err or not text:
            raise RuntimeError(f"Could not extract PDF text from {path.name}: {err or 'empty'}")
        return text

    raw = path.read_text(encoding="utf-8", errors="replace")
    if suffix in {".html", ".htm", ".eml"}:
        raw = _strip_html(raw)
    if max_chars:
        raw = raw[:max_chars]
    return raw


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(
    text: str,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    text = normalize_text(text)
    if not text:
        return []
    if len(text) <= chunk_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_chars)
        if end < len(text):
            split_at = max(
                text.rfind("\n\n", start + chunk_chars // 2, end),
                text.rfind(". ", start + chunk_chars // 2, end),
                text.rfind("\n", start + chunk_chars // 2, end),
            )
            if split_at > start:
                end = split_at + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 4))


def _load_companies() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _theme_names() -> list[str]:
    base = DATA_DIR / "Thematic"
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith("_"))


def _author_names(category: str) -> list[str]:
    base = DATA_DIR / category / "authors"
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith("_"))


def extract_entities(text: str, title: str = "", metadata: dict | None = None) -> dict:
    metadata = metadata or {}
    haystack = f"{title}\n{text[:20000]}".lower()
    companies = _load_companies()

    tickers = set(metadata.get("tickers") or [])
    company_names = []
    for ticker, info in companies.items():
        name = (info.get("name") or "").strip()
        ticker_l = ticker.lower()
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(ticker_l)}(?![A-Za-z0-9])", haystack):
            tickers.add(ticker)
        elif name and len(name) > 4 and name.lower() in haystack:
            tickers.add(ticker)
        if ticker in tickers and name:
            company_names.append(name)

    themes = set(metadata.get("themes") or [])
    for theme in _theme_names():
        if theme.lower() in haystack:
            themes.add(theme)

    authors = set(metadata.get("authors") or [])
    for category in ("Macro", "Semis"):
        for author in _author_names(category):
            if author.lower() in haystack:
                authors.add(author)

    return {
        "tickers": sorted(tickers),
        "company_names": sorted(set(company_names)),
        "themes": sorted(themes),
        "authors": sorted(authors),
    }


def source_type_for_path(path: str | Path) -> str:
    path = Path(path)
    try:
        rel = path.resolve().relative_to(DATA_DIR.resolve())
        parts = rel.parts
    except ValueError:
        parts = path.parts

    part_set = {p.lower() for p in parts}
    if "_claude_memory" in part_set:
        return "claude"
    if "_email" in part_set:
        return "email"
    if "_headlines" in part_set:
        return "headlines"
    if "_skills" in part_set:
        return "skills"
    if "_notes" in part_set:
        return "note"
    if "macro" in part_set:
        return "macro"
    if "semis" in part_set:
        return "semis"
    if "thematic" in part_set:
        return "thematic"
    if "research" in part_set:
        return "research"
    if "ir" in part_set:
        return "ir"
    if part_set.intersection({"sec", "edinet", "dart", "mops", "tdnet", "filings"}):
        return "filing"
    return "file"


def _source_uri_for_file(path: Path, source_type: str) -> str:
    try:
        rel = path.resolve().relative_to(DATA_DIR.resolve())
        return f"{source_type}:{rel.as_posix()}"
    except ValueError:
        return f"{source_type}:{path.resolve().as_posix()}"


class EmbeddingClient:
    def __init__(self, provider: str | None = None, model: str | None = None):
        self.provider = (provider or os.environ.get("KB_EMBEDDING_PROVIDER") or "openai").lower()
        self.model = model or os.environ.get("KB_EMBEDDING_MODEL") or "text-embedding-3-small"
        self._client = None

    @property
    def enabled(self) -> bool:
        return self.provider == "openai"

    def embed_texts(self, texts: list[str]) -> list[list[float] | None]:
        if not texts:
            return []
        if not self.enabled:
            return [None for _ in texts]
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is required for KB_EMBEDDING_PROVIDER=openai")
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI()

        out: list[list[float] | None] = []
        batch_size = 64
        for i in range(0, len(texts), batch_size):
            batch = [t.replace("\n", " ")[:28000] for t in texts[i:i + batch_size]]
            resp = self._client.embeddings.create(
                model=self.model,
                input=batch,
                encoding_format="float",
            )
            vectors = [item.embedding for item in resp.data]
            out.extend(vectors)
        return out


def _vector_dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _vector_norm(a: list[float]) -> float:
    return math.sqrt(sum(x * x for x in a)) or 1.0


def _cosine(a: list[float], b: list[float]) -> float:
    return _vector_dot(a, b) / (_vector_norm(a) * _vector_norm(b))


def index_text(
    *,
    title: str,
    text: str,
    source_type: str,
    source_uri: str,
    source_path: str | None = None,
    url: str | None = None,
    author: str | None = None,
    publisher: str | None = None,
    metadata: dict | None = None,
    entities: dict | None = None,
    force: bool = False,
    embed: bool = True,
    conn: sqlite3.Connection | None = None,
) -> dict:
    close_conn = conn is None
    conn = conn or connect()
    metadata = metadata or {}
    text = normalize_text(text)
    h = text_hash(text)
    now = _now()

    existing = conn.execute(
        "SELECT id, hash FROM documents WHERE source_uri = ?",
        (source_uri,),
    ).fetchone()
    if existing and existing["hash"] == h and not force:
        if close_conn:
            conn.close()
        return {"indexed": False, "document_id": existing["id"], "chunks": 0, "reason": "unchanged"}

    if entities is None:
        entities = extract_entities(text, title=title, metadata=metadata)
    chunks = chunk_text(text)
    if not chunks:
        if close_conn:
            conn.close()
        return {"indexed": False, "document_id": existing["id"] if existing else None, "chunks": 0, "reason": "empty"}

    if existing:
        doc_id = int(existing["id"])
        conn.execute(
            """
            UPDATE documents
            SET source_type = ?, title = ?, source_path = ?, url = ?, author = ?,
                publisher = ?, hash = ?, entities_json = ?, metadata_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                source_type,
                title,
                source_path,
                url,
                author,
                publisher,
                h,
                _json(entities),
                _json(metadata),
                now,
                doc_id,
            ),
        )
        old_chunk_ids = [r["id"] for r in conn.execute("SELECT id FROM chunks WHERE document_id = ?", (doc_id,))]
        if old_chunk_ids:
            conn.executemany("DELETE FROM chunks_fts WHERE chunk_id = ?", [(cid,) for cid in old_chunk_ids])
        conn.execute("DELETE FROM chunks WHERE document_id = ?", (doc_id,))
    else:
        cur = conn.execute(
            """
            INSERT INTO documents
                (source_uri, source_type, title, source_path, url, author, publisher,
                 hash, entities_json, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_uri,
                source_type,
                title,
                source_path,
                url,
                author,
                publisher,
                h,
                _json(entities),
                _json(metadata),
                now,
                now,
            ),
        )
        doc_id = int(cur.lastrowid)

    embeddings: list[list[float] | None]
    model = None
    if embed:
        try:
            emb = EmbeddingClient()
            embeddings = emb.embed_texts(chunks)
            model = emb.model if emb.enabled else None
        except Exception as e:
            metadata = {**metadata, "embedding_error": str(e)}
            embeddings = [None for _ in chunks]
    else:
        embeddings = [None for _ in chunks]

    entity_text = " ".join(
        entities.get("tickers", [])
        + entities.get("company_names", [])
        + entities.get("themes", [])
        + entities.get("authors", [])
    )

    for idx, chunk in enumerate(chunks):
        vector = embeddings[idx] if idx < len(embeddings) else None
        cur = conn.execute(
            """
            INSERT INTO chunks
                (document_id, chunk_index, text, token_estimate, embedding_model,
                 embedding_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc_id,
                idx,
                chunk,
                _estimate_tokens(chunk),
                model if vector is not None else None,
                json.dumps(vector) if vector is not None else None,
                now,
            ),
        )
        chunk_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO chunks_fts(chunk_id, text, title, source_type, entity_text) VALUES (?, ?, ?, ?, ?)",
            (chunk_id, chunk, title or "", source_type, entity_text),
        )

    conn.commit()
    if close_conn:
        conn.close()
    return {"indexed": True, "document_id": doc_id, "chunks": len(chunks), "reason": "updated" if existing else "new"}


def index_file(
    path: str | Path,
    source_type: str | None = None,
    force: bool = False,
    metadata: dict | None = None,
    embed: bool = True,
    conn: sqlite3.Connection | None = None,
) -> dict:
    path = Path(path)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(path)
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        return {"indexed": False, "chunks": 0, "reason": "unsupported_suffix", "path": str(path)}
    source_type = source_type or source_type_for_path(path)
    text = extract_text_from_file(path)
    title = metadata.get("title") if metadata else None
    title = title or path.stem
    source_uri = (metadata or {}).get("source_uri") or _source_uri_for_file(path, source_type)
    return index_text(
        title=title,
        text=text,
        source_type=source_type,
        source_uri=source_uri,
        source_path=str(path),
        metadata=metadata or {},
        force=force,
        embed=embed,
        conn=conn,
    )


def _iter_files_for_source(source: str) -> Iterable[Path]:
    if not DATA_DIR.exists():
        return []
    source = source.lower()
    if source == "all":
        seen = set()
        for sub in ("research", "ir", "filings", "claude", "email", "headlines", "skills", "notes"):
            for path in _iter_files_for_source(sub):
                if path not in seen:
                    seen.add(path)
                    yield path
        return

    if source == "research":
        patterns = [
            "*/*research*/notes/*",
            "Macro/authors/*/notes/*",
            "Semis/authors/*/notes/*",
            "Thematic/*/notes/*",
        ]
    elif source == "ir":
        patterns = ["*/ir/**/*"]
    elif source in {"filing", "filings", "company"}:
        patterns = ["*/filings/**/*", "*/sec/**/*", "*/edinet/**/*", "*/dart/**/*", "*/mops/**/*", "*/tdnet/**/*", "*/ir/**/*"]
    elif source == "claude":
        patterns = ["_claude_memory/**/*"]
    elif source == "email":
        patterns = ["_email/**/*"]
    elif source in {"headline", "headlines"}:
        patterns = ["_headlines/**/*"]
    elif source in {"note", "notes"}:
        patterns = ["_notes/**/*"]
    elif source in {"skill", "skills"}:
        patterns = ["_skills/**/*"]
    else:
        patterns = ["**/*"]

    for pattern in patterns:
        for path in DATA_DIR.glob(pattern):
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES and "_kb" not in path.parts:
                yield path


def reindex_source(source: str = "all", force: bool = False, limit: int = 0, embed: bool = True) -> dict:
    conn = connect()
    stats = {"source": source, "scanned": 0, "indexed": 0, "skipped": 0, "errors": []}
    try:
        for path in _iter_files_for_source(source):
            if limit and stats["scanned"] >= limit:
                break
            stats["scanned"] += 1
            try:
                result = index_file(path, force=force, embed=embed, conn=conn)
                if result.get("indexed"):
                    stats["indexed"] += 1
                else:
                    stats["skipped"] += 1
            except Exception as e:
                stats["errors"].append({"path": str(path), "error": f"{type(e).__name__}: {e}"})
    finally:
        conn.close()
    return stats


def _fts_query(query: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9_.$-]+", query)
    tokens = [t.strip("-") for t in tokens if len(t.strip("-")) >= 2]
    return " OR ".join(tokens[:12]) or query.replace('"', '""')


def _source_where(sources: str) -> tuple[str, list[str]]:
    sources = (sources or "all").lower()
    if sources == "all":
        return "", []
    mapping = {
        "research": ["research", "macro", "semis", "thematic", "note"],
        "claude": ["claude"],
        "company": ["ir", "filing", "research"],
        "headlines": ["headlines"],
        "skills": ["skills"],
        "email": ["email"],
    }
    allowed = mapping.get(sources, [sources])
    placeholders = ",".join("?" for _ in allowed)
    return f" AND d.source_type IN ({placeholders})", allowed


def search(query: str, sources: str = "all", limit: int = DEFAULT_SEARCH_LIMIT) -> list[dict]:
    conn = connect()
    try:
        where_sql, params = _source_where(sources)
        fts = _fts_query(query)
        rows = conn.execute(
            f"""
            SELECT c.id AS chunk_id, c.text, c.embedding_json, d.id AS document_id,
                   d.title, d.source_type, d.source_path, d.url, d.entities_json,
                   d.metadata_json, bm25(chunks_fts) AS rank
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.chunk_id
            JOIN documents d ON d.id = c.document_id
            WHERE chunks_fts MATCH ? {where_sql}
            ORDER BY rank
            LIMIT ?
            """,
            [fts, *params, max(limit * 3, limit)],
        ).fetchall()

        results: dict[int, dict] = {}
        for pos, row in enumerate(rows):
            score = 1.0 / (1.0 + max(float(row["rank"]), 0.0)) + max(0.0, 0.25 - pos * 0.01)
            results[int(row["chunk_id"])] = _row_to_result(row, score, "fts")

        vector_rows = conn.execute(
            f"""
            SELECT c.id AS chunk_id, c.text, c.embedding_json, d.id AS document_id,
                   d.title, d.source_type, d.source_path, d.url, d.entities_json,
                   d.metadata_json
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.embedding_json IS NOT NULL {where_sql}
            """,
            params,
        ).fetchall()
        if vector_rows:
            try:
                q_vec = EmbeddingClient().embed_texts([query])[0]
            except Exception:
                q_vec = None
            if q_vec is not None:
                scored = []
                for row in vector_rows:
                    vec = json.loads(row["embedding_json"])
                    scored.append((_cosine(q_vec, vec), row))
                for score, row in sorted(scored, key=lambda x: x[0], reverse=True)[: max(limit * 3, limit)]:
                    chunk_id = int(row["chunk_id"])
                    if chunk_id in results:
                        results[chunk_id]["score"] += float(score)
                        results[chunk_id]["match"] = "hybrid"
                    else:
                        results[chunk_id] = _row_to_result(row, float(score), "vector")

        ordered = sorted(results.values(), key=lambda r: r["score"], reverse=True)
        return ordered[:limit]
    finally:
        conn.close()


def _row_to_result(row: sqlite3.Row, score: float, match: str) -> dict:
    return {
        "chunk_id": int(row["chunk_id"]),
        "document_id": int(row["document_id"]),
        "title": row["title"],
        "source_type": row["source_type"],
        "source_path": row["source_path"],
        "url": row["url"],
        "entities": _loads(row["entities_json"], {}),
        "metadata": _loads(row["metadata_json"], {}),
        "text": row["text"],
        "score": score,
        "match": match,
    }


def format_results(results: list[dict]) -> str:
    if not results:
        return "No matching KB results."
    lines = []
    for i, result in enumerate(results, start=1):
        source = result.get("source_path") or result.get("url") or "unknown source"
        source_name = Path(source).name if source and "://" not in source else source
        entities = result.get("entities") or {}
        entity_text = ", ".join(entities.get("tickers", []) + entities.get("themes", []))
        entity_suffix = f" | {entity_text}" if entity_text else ""
        snippet = normalize_text(result.get("text", ""))[:220]
        lines.append(
            f"[{i}] {result['title']} ({result['source_type']}, score={result['score']:.2f}{entity_suffix})\n"
            f"{source_name}\n"
            f"{snippet}"
        )
    return "\n\n".join(lines)


def ask(question: str, sources: str = "all", limit: int = DEFAULT_SEARCH_LIMIT) -> str:
    results = search(question, sources=sources, limit=limit)
    if not results:
        return "I could not find anything relevant in the KB yet."

    context_blocks = []
    for i, result in enumerate(results, start=1):
        source = result.get("source_path") or result.get("url") or "unknown source"
        context_blocks.append(
            f"[S{i}] title={result['title']!r} source_type={result['source_type']} source={source}\n"
            f"{result['text'][:2500]}"
        )

    prompt = f"""You are answering a buy-side research question using a local knowledge base.

Rules:
- Use only the supplied sources unless you clearly label a statement as inference.
- Cite sources inline as [S1], [S2], etc.
- Separate high-confidence facts from implications.
- Be concise but specific with tickers, dates, numbers, and caveats.

Question:
{question}

Sources:
{chr(10).join(context_blocks)}
"""
    try:
        from anthropic import Anthropic

        model = os.environ.get("KB_SYNTHESIS_MODEL", "claude-sonnet-4-6")
        client = Anthropic(timeout=180.0, max_retries=3)
        resp = client.messages.create(
            model=model,
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        answer = resp.content[0].text
    except Exception as e:
        answer = (
            f"KB retrieval succeeded, but synthesis failed: {type(e).__name__}: {e}\n\n"
            f"Top matches:\n\n{format_results(results)}"
        )

    citations = []
    for i, result in enumerate(results, start=1):
        source = result.get("source_path") or result.get("url") or "unknown source"
        citations.append(f"[S{i}] {result['title']} - {source}")
    return answer.strip() + "\n\nSources:\n" + "\n".join(citations)
