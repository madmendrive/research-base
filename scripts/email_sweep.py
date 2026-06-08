"""IMAP research inbox sweep for Substack/research emails."""

from __future__ import annotations

import email
import imaplib
import json
import os
from datetime import datetime
from email.header import decode_header
from email.message import Message
from pathlib import Path

from scripts import kb
from scripts.jobs import enqueue_job
from scripts.notify import telegram_send

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
EMAIL_DIR = DATA_DIR / "_email"
STATE_PATH = EMAIL_DIR / "_email_state.json"


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {"processed": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"processed": {}}


def _save_state(state: dict) -> None:
    EMAIL_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = []
    for data, charset in decode_header(value):
        if isinstance(data, bytes):
            parts.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(data)
    return "".join(parts)


def _message_body(msg: Message) -> tuple[str, str]:
    html_part = ""
    text_part = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="replace")
            if ctype == "text/html" and not html_part:
                html_part = body
            elif ctype == "text/plain" and not text_part:
                text_part = body
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            text_part = payload.decode(charset, errors="replace")
    if html_part:
        return kb._strip_html(html_part), html_part
    return text_part, ""


def _save_attachments(msg: Message, dest_dir: Path) -> list[Path]:
    paths = []
    for part in msg.walk():
        disp = (part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        if "attachment" not in disp and not filename:
            continue
        filename = kb.slugify(_decode(filename) or "attachment", max_len=120)
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        dest = dest_dir / filename
        if dest.exists():
            dest = dest_dir / f"{datetime.now().strftime('%H%M%S')}_{filename}"
        dest.write_bytes(payload)
        paths.append(dest)
    return paths


def _connect():
    host = os.environ.get("RESEARCH_IMAP_HOST")
    username = os.environ.get("RESEARCH_IMAP_USERNAME")
    password = os.environ.get("RESEARCH_IMAP_PASSWORD")
    port = int(os.environ.get("RESEARCH_IMAP_PORT", "993"))
    mailbox = os.environ.get("RESEARCH_IMAP_MAILBOX", "INBOX")
    if not host or not username or not password:
        raise RuntimeError("RESEARCH_IMAP_HOST, RESEARCH_IMAP_USERNAME, and RESEARCH_IMAP_PASSWORD are required")
    client = imaplib.IMAP4_SSL(host, port)
    client.login(username, password)
    client.select(mailbox)
    return client, mailbox


def email_sweep(notify: bool = False, limit: int = 50, analyse_attachments: bool = False) -> dict:
    EMAIL_DIR.mkdir(parents=True, exist_ok=True)
    state = _load_state()
    processed = state.setdefault("processed", {})
    stats = {"seen": 0, "new": 0, "indexed": 0, "attachments": 0, "queued_pdfs": 0}

    client, mailbox = _connect()
    try:
        status, data = client.uid("SEARCH", None, "ALL")
        if status != "OK":
            raise RuntimeError(f"IMAP search failed: {status}")
        uids = data[0].split()[-limit:]
        for uid_b in uids:
            uid = uid_b.decode()
            stats["seen"] += 1
            if uid in processed:
                continue
            status, msg_data = client.uid("FETCH", uid, "(RFC822)")
            if status != "OK" or not msg_data:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            subject = _decode(msg.get("Subject"))
            sender = _decode(msg.get("From"))
            date = _decode(msg.get("Date"))
            message_id = msg.get("Message-ID") or uid
            body_text, body_html = _message_body(msg)
            if not body_text.strip():
                processed[uid] = {"skipped": "empty", "message_id": message_id}
                continue

            day = datetime.now().strftime("%Y-%m-%d")
            dest_dir = EMAIL_DIR / day / kb.slugify(subject or uid, max_len=80)
            dest_dir.mkdir(parents=True, exist_ok=True)
            md_path = dest_dir / "message.md"
            md = (
                f"# {subject or '(no subject)'}\n\n"
                f"- Source: email\n"
                f"- From: {sender}\n"
                f"- Date: {date}\n"
                f"- Message-ID: {message_id}\n"
                f"- Mailbox: {mailbox}\n\n"
                f"{body_text.strip()}\n"
            )
            md_path.write_text(md, encoding="utf-8")
            if body_html:
                (dest_dir / "message.html").write_text(body_html, encoding="utf-8")
            attachments = _save_attachments(msg, dest_dir)
            stats["attachments"] += len(attachments)

            result = kb.index_text(
                title=subject or uid,
                text=md,
                source_type="email",
                source_uri=f"email:{message_id}",
                source_path=str(md_path),
                author=sender,
                metadata={"uid": uid, "mailbox": mailbox, "date": date},
                force=True,
            )
            if result.get("indexed"):
                stats["indexed"] += 1
            for attachment in attachments:
                if attachment.suffix.lower() == ".pdf":
                    enqueue_job(
                        "ingest_file",
                        {"path": str(attachment), "notify": analyse_attachments},
                        dedupe_key=f"ingest_file:{kb.file_hash(attachment)}",
                    )
                    stats["queued_pdfs"] += 1
            processed[uid] = {
                "message_id": message_id,
                "subject": subject,
                "processed_at": datetime.now().isoformat(timespec="seconds"),
            }
            stats["new"] += 1
            _save_state(state)
    finally:
        try:
            client.logout()
        except Exception:
            pass

    if notify and stats["new"]:
        telegram_send(
            f"Email sweep: {stats['new']} new message(s), "
            f"{stats['queued_pdfs']} PDF attachment(s) queued."
        )
    return stats
