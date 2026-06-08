"""Import official Claude chat-history exports into the local KB."""

from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path

from scripts import kb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CLAUDE_DIR = DATA_DIR / "_claude_memory"


def _iter_json_payloads(path: Path):
    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".json"):
                    try:
                        yield name, json.loads(zf.read(name).decode("utf-8", errors="replace"))
                    except Exception:
                        continue
        return

    if path.is_dir():
        for item in path.rglob("*.json"):
            try:
                yield str(item.relative_to(path)), json.loads(item.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
        return

    if path.is_file() and path.suffix.lower() == ".json":
        yield path.name, json.loads(path.read_text(encoding="utf-8", errors="replace"))


def _candidate_conversations(payload):
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield item
        return
    if not isinstance(payload, dict):
        return

    for key in ("conversations", "chats", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield item
    if any(k in payload for k in ("chat_messages", "messages", "name", "title", "uuid", "id")):
        yield payload


def _content_to_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
                elif isinstance(item.get("name"), str):
                    parts.append(f"[attachment: {item['name']}]")
        return "\n".join(parts)
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        if isinstance(content.get("content"), str):
            return content["content"]
    return str(content)


def _message_text(message: dict) -> tuple[str, str, str]:
    role = (
        message.get("sender")
        or message.get("role")
        or message.get("author")
        or message.get("from")
        or "unknown"
    )
    created = message.get("created_at") or message.get("createdAt") or message.get("timestamp") or ""
    text = (
        _content_to_text(message.get("text"))
        or _content_to_text(message.get("content"))
        or _content_to_text(message.get("message"))
    )
    return str(role), str(created), text.strip()


def _conversation_messages(conv: dict) -> list[dict]:
    for key in ("chat_messages", "messages", "conversation", "turns"):
        value = conv.get(key)
        if isinstance(value, list):
            return [m for m in value if isinstance(m, dict)]
    return []


def _conversation_title(conv: dict) -> str:
    return (
        conv.get("name")
        or conv.get("title")
        or conv.get("summary")
        or conv.get("uuid")
        or conv.get("id")
        or "Claude conversation"
    )


def _conversation_date(conv: dict) -> str:
    raw = conv.get("created_at") or conv.get("createdAt") or conv.get("updated_at") or conv.get("updatedAt")
    if not raw:
        return datetime.now().strftime("%Y-%m-%d")
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return str(raw)[:10] or datetime.now().strftime("%Y-%m-%d")


def _conversation_id(conv: dict, fallback: str) -> str:
    return str(conv.get("uuid") or conv.get("id") or conv.get("conversation_id") or fallback)


def _conversation_markdown(conv: dict) -> tuple[str, dict]:
    title = _conversation_title(conv)
    date = _conversation_date(conv)
    conv_id = _conversation_id(conv, kb.text_hash(title + date)[:12])
    messages = _conversation_messages(conv)
    lines = [
        f"# {title}",
        "",
        f"- Source: Claude export",
        f"- Conversation ID: {conv_id}",
        f"- Date: {date}",
        "",
    ]
    for msg in messages:
        role, created, text = _message_text(msg)
        if not text:
            continue
        stamp = f" ({created})" if created else ""
        lines.append(f"## {role}{stamp}")
        lines.append("")
        lines.append(text)
        lines.append("")
    metadata = {
        "title": title,
        "date": date,
        "conversation_id": conv_id,
        "message_count": len(messages),
        "source": "claude_export",
    }
    return "\n".join(lines).strip() + "\n", metadata


def _classify_memory_priority(text: str) -> str:
    low = text[:50000].lower()
    high_terms = [
        "semiconductor",
        "semis",
        "nvidia",
        "tsmc",
        "ai infrastructure",
        "memory",
        "gpu",
        "foundry",
        "earnings",
        "stocks",
        "equity",
        "macro",
        "portfolio",
    ]
    return "high" if any(term in low for term in high_terms) else "normal"


def import_claude_export(path: str | Path, dry_run: bool = False, force: bool = False) -> dict:
    src = Path(path).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(src)

    conversations = []
    for payload_name, payload in _iter_json_payloads(src):
        for conv in _candidate_conversations(payload):
            messages = _conversation_messages(conv)
            if messages:
                conversations.append((payload_name, conv))

    stats = {"source": str(src), "conversations": len(conversations), "written": 0, "indexed": 0, "dry_run": dry_run}
    if dry_run:
        return stats

    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    conn = kb.connect()
    try:
        for payload_name, conv in conversations:
            md, metadata = _conversation_markdown(conv)
            metadata["payload_name"] = payload_name
            metadata["memory_priority"] = _classify_memory_priority(md)
            filename = f"{metadata['date']}_{kb.slugify(metadata['title'])}_{metadata['conversation_id'][:8]}.md"
            dest = CLAUDE_DIR / filename
            if force or not dest.exists() or dest.read_text(encoding="utf-8", errors="replace") != md:
                dest.write_text(md, encoding="utf-8")
                stats["written"] += 1
            result = kb.index_text(
                title=metadata["title"],
                text=md,
                source_type="claude",
                source_uri=f"claude:{metadata['conversation_id']}",
                source_path=str(dest),
                metadata=metadata,
                force=force,
                conn=conn,
            )
            if result.get("indexed"):
                stats["indexed"] += 1
    finally:
        conn.close()
    return stats
