"""Adaptive analyst learning memory.

This is deliberately not self-modifying code. It is a durable feedback and
lesson layer that the analyst prompt reads on every answer.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

from scripts import kb


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value) -> str:
    return json.dumps({} if value is None else value, ensure_ascii=False, sort_keys=True)


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS analyst_lessons (
            id INTEGER PRIMARY KEY,
            lesson_text TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'user',
            status TEXT NOT NULL DEFAULT 'active',
            weight REAL NOT NULL DEFAULT 1.0,
            tags_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS analyst_interactions (
            id INTEGER PRIMARY KEY,
            channel TEXT NOT NULL,
            user_id TEXT,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS analyst_feedback (
            id INTEGER PRIMARY KEY,
            interaction_id INTEGER REFERENCES analyst_interactions(id) ON DELETE SET NULL,
            user_id TEXT,
            rating TEXT,
            feedback_text TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_analyst_lessons_status ON analyst_lessons(status, weight);
        CREATE INDEX IF NOT EXISTS idx_analyst_interactions_user ON analyst_interactions(user_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_analyst_feedback_interaction ON analyst_feedback(interaction_id);
        """
    )
    conn.commit()


def add_lesson(
    lesson_text: str,
    *,
    source: str = "user",
    tags: list[str] | None = None,
    weight: float = 1.0,
    conn: sqlite3.Connection | None = None,
) -> int:
    lesson_text = (lesson_text or "").strip()
    if not lesson_text:
        raise ValueError("lesson_text is required")
    close_conn = conn is None
    conn = conn or kb.connect()
    init_schema(conn)
    now = _now()
    try:
        cur = conn.execute(
            """
            INSERT INTO analyst_lessons
                (lesson_text, source, status, weight, tags_json, created_at, updated_at)
            VALUES (?, ?, 'active', ?, ?, ?, ?)
            """,
            (lesson_text, source, float(weight), _json(tags or []), now, now),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        if close_conn:
            conn.close()


def list_lessons(limit: int = 20, conn: sqlite3.Connection | None = None) -> list[dict]:
    close_conn = conn is None
    conn = conn or kb.connect()
    init_schema(conn)
    try:
        rows = conn.execute(
            """
            SELECT id, lesson_text, source, weight, tags_json, created_at, updated_at
            FROM analyst_lessons
            WHERE status = 'active'
            ORDER BY weight DESC, updated_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if close_conn:
            conn.close()


def format_lessons(limit: int = 20) -> str:
    rows = list_lessons(limit=limit)
    if not rows:
        return "No active analyst lessons yet."
    lines = ["Active analyst lessons:"]
    for row in rows:
        lines.append(f"- #{row['id']}: {row['lesson_text']} ({row['source']})")
    return "\n".join(lines)


def log_interaction(
    question: str,
    answer: str,
    *,
    channel: str = "telegram",
    user_id: str | int | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    close_conn = conn is None
    conn = conn or kb.connect()
    init_schema(conn)
    try:
        cur = conn.execute(
            """
            INSERT INTO analyst_interactions (channel, user_id, question, answer, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (channel, str(user_id) if user_id is not None else None, question, answer, _now()),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        if close_conn:
            conn.close()


def latest_interaction_id(user_id: str | int | None, conn: sqlite3.Connection | None = None) -> int | None:
    if user_id is None:
        return None
    close_conn = conn is None
    conn = conn or kb.connect()
    init_schema(conn)
    try:
        row = conn.execute(
            """
            SELECT id FROM analyst_interactions
            WHERE user_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (str(user_id),),
        ).fetchone()
        return int(row["id"]) if row else None
    finally:
        if close_conn:
            conn.close()


def record_feedback(
    feedback_text: str,
    *,
    user_id: str | int | None = None,
    rating: str | None = None,
    interaction_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    feedback_text = (feedback_text or "").strip()
    if not feedback_text:
        raise ValueError("feedback_text is required")
    close_conn = conn is None
    conn = conn or kb.connect()
    init_schema(conn)
    if interaction_id is None:
        interaction_id = latest_interaction_id(user_id, conn=conn)
    try:
        cur = conn.execute(
            """
            INSERT INTO analyst_feedback
                (interaction_id, user_id, rating, feedback_text, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (interaction_id, str(user_id) if user_id is not None else None, rating, feedback_text, _now()),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        if close_conn:
            conn.close()


def _query_terms(query: str) -> list[str]:
    terms = []
    for term in re.findall(r"[A-Za-z0-9.$_-]{3,}", query or ""):
        term = term.lower().strip("._-$")
        if term and term not in terms:
            terms.append(term)
        if len(terms) >= 12:
            break
    return terms


def learning_context(
    query: str = "",
    lesson_limit: int = 12,
    feedback_limit: int = 5,
    conn: sqlite3.Connection | None = None,
) -> str:
    close_conn = conn is None
    conn = conn or kb.connect()
    init_schema(conn)
    try:
        lessons = list_lessons(limit=lesson_limit, conn=conn)
        feedback_rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT f.rating, f.feedback_text, f.created_at, i.question
                FROM analyst_feedback f
                LEFT JOIN analyst_interactions i ON i.id = f.interaction_id
                ORDER BY f.created_at DESC, f.id DESC
                LIMIT ?
                """,
                (feedback_limit,),
            )
        ]
    finally:
        if close_conn:
            conn.close()

    if not lessons and not feedback_rows:
        return ""

    terms = _query_terms(query)
    def score_lesson(row: dict) -> tuple[int, float]:
        text = (row.get("lesson_text") or "").lower()
        matches = sum(1 for term in terms if term in text)
        return matches, float(row.get("weight") or 1.0)

    lessons = sorted(lessons, key=score_lesson, reverse=True)[:lesson_limit]
    lines = ["<analyst_learning_memory>"]
    if lessons:
        lines.append("Reusable analyst lessons and user preferences:")
        for row in lessons:
            lines.append(f"- {row.get('lesson_text')}")
    if feedback_rows:
        lines.append("Recent user feedback to internalize:")
        for row in feedback_rows:
            prefix = f"{row.get('rating')}: " if row.get("rating") else ""
            question = row.get("question")
            if question and len(question) > 140:
                question = question[:137] + "..."
            tail = f" Prior question: {question}" if question else ""
            lines.append(f"- {prefix}{row.get('feedback_text')}.{tail}")
    lines.append("</analyst_learning_memory>")
    return "\n".join(lines)
