"""Research inbox sweep for Substack/research emails.

Supports either Gmail API OAuth or IMAP. Both paths feed the same MIME parser,
attachment saver, KB indexer, and structured research-memory extractor.
"""

from __future__ import annotations

import base64
import email
import imaplib
import json
import os
from datetime import datetime
from email import policy
from email.header import decode_header
from email.message import Message
from pathlib import Path

from scripts import kb
from scripts.jobs import enqueue_job
from scripts.notify import telegram_send, telegram_send_markdownish_html

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
EMAIL_DIR = DATA_DIR / "_email"
STATE_PATH = EMAIL_DIR / "_email_state.json"


def _latest_digest_path() -> Path:
    # Derived from EMAIL_DIR at call time so patching EMAIL_DIR (tests) moves
    # the digest file too, without a separate constant to keep in sync.
    return EMAIL_DIR / "_latest_email_digest.json"
SECRETS_DIR = DATA_DIR / "_secrets"
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
MAX_EML_DEPTH = 3
NON_RESEARCH_SUBJECT_PATTERNS = (
    "payment receipt",
    "subscription receipt",
    "welcome to",
    "welcome.",
    "welcome!",
    "subscriptions for you to give away",
    "did you forget to send your gifts",
    "here's $50",
    "here’s $50",
    "substack verification code",
    "new thread from",
    "pricing update",
    "last call",
    "personal update",
)


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


def _provider() -> str:
    return (os.environ.get("RESEARCH_EMAIL_PROVIDER") or "gmail_api").strip().lower()


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
    def collect(part: Message) -> tuple[str, str]:
        disp = (part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        ctype = part.get_content_type()
        if "attachment" in disp or filename or ctype == "message/rfc822":
            return "", ""
        if part.is_multipart():
            text_part = ""
            html_part = ""
            payload = part.get_payload()
            if isinstance(payload, list):
                for child in payload:
                    child_text, child_html = collect(child)
                    if child_html and not html_part:
                        html_part = child_html
                    if child_text and not text_part:
                        text_part = child_text
            return text_part, html_part
        payload = part.get_payload(decode=True)
        if not payload:
            return "", ""
        charset = part.get_content_charset() or "utf-8"
        body = payload.decode(charset, errors="replace")
        if ctype == "text/html":
            return "", body
        if ctype == "text/plain":
            return body, ""
        return "", ""

    text_part, html_part = collect(msg)
    if html_part:
        return kb._strip_html(html_part), html_part
    return text_part, ""


def _safe_filename(filename: str | None, fallback: str = "attachment") -> str:
    name = _decode(filename) or fallback
    return kb.slugify(name, max_len=120)


def _has_attachments(msg: Message) -> bool:
    for part in msg.walk():
        if part is msg:
            continue
        disp = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disp or part.get_filename() or part.get_content_type() == "message/rfc822":
            return True
    return False


def _is_low_value_email(subject: str, sender: str = "", body_text: str = "") -> bool:
    subject_l = (subject or "").strip().lower()
    if not subject_l:
        return False
    if any(pattern in subject_l for pattern in NON_RESEARCH_SUBJECT_PATTERNS):
        return True
    if subject_l.endswith(" welcome"):
        return True
    if "is your substack verification code" in subject_l:
        return True
    if "receipt from" in subject_l and "substack" in ((sender or "") + " " + body_text).lower():
        return True
    return False


def _save_attachments(msg: Message, dest_dir: Path) -> list[Path]:
    paths = []
    for part in msg.walk():
        disp = (part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        ctype = part.get_content_type()
        if "attachment" not in disp and not filename and ctype != "message/rfc822":
            continue
        payload = part.get_payload(decode=True)
        if not payload and ctype == "message/rfc822":
            nested = part.get_payload()
            if isinstance(nested, list):
                payload = b"\n".join(
                    item.as_bytes(policy=policy.default)
                    for item in nested
                    if hasattr(item, "as_bytes")
                )
            elif hasattr(nested, "as_bytes"):
                payload = nested.as_bytes(policy=policy.default)
            elif isinstance(nested, str):
                payload = nested.encode("utf-8", errors="replace")
        if not payload:
            continue
        filename = _safe_filename(filename, fallback="attached_email.eml")
        if ctype == "message/rfc822" and not filename.lower().endswith(".eml"):
            filename = f"{filename}.eml"
        dest = dest_dir / filename
        if dest.exists():
            dest = dest_dir / f"{datetime.now().strftime('%H%M%S')}_{filename}"
        dest.write_bytes(payload)
        paths.append(dest)
    return paths


def _memory_scope_from_triage(triage: dict) -> dict:
    primary_type = triage.get("primary_type")
    subject = triage.get("primary_subject") or "Email Research"
    category = triage.get("category")
    if primary_type == "single_name":
        return {"corpus_type": "single_name", "subject_type": "ticker", "subject": subject}
    if primary_type == "thematic":
        return {"corpus_type": "thematic", "subject_type": "theme", "subject": subject}
    if primary_type == "macro":
        return {"corpus_type": "macro", "subject_type": "author", "subject": subject}
    if category == "Semis":
        return {"corpus_type": "semis", "subject_type": "author", "subject": subject}
    return {"corpus_type": "research", "subject_type": "author", "subject": subject}


_GENERIC_SENDER_NAMES = {
    "substack", "newsletter", "noreply", "no-reply", "no reply", "mail",
    "email", "notifications", "updates", "info", "admin", "support",
}


def _sender_display_name(sender: str) -> str:
    """Display name from a From header ('Name <addr>' -> 'Name'), cleaned.

    Returns "" when there is no usable publication/author name (bare address,
    generic mailer names, junk)."""
    import re

    name = (sender or "").split("<", 1)[0]
    name = name.replace("\r", " ").replace("\n", " ").strip().strip('"').strip("'").strip()
    # observed artifact: "Global Semi Research from Global Semi Research"
    m = re.match(r"^(?P<a>.+?)\s+from\s+(?P=a)$", name, flags=re.IGNORECASE)
    if m:
        name = m.group("a").strip()
    name = re.sub(r"\s+", " ", name)
    if not name or "@" in name or len(name) < 3 or len(name) > 80:
        return ""
    if name.lower() in _GENERIC_SENDER_NAMES:
        return ""
    return name


def _reconcile_author_scope(scope: dict, sender: str) -> dict:
    """Trust the sender display name over triage's author pick when they
    disagree.

    Triage's author inventory is the data/{Macro,Semis}/authors dir listing,
    so an email-only publication not yet in it gets snapped to the nearest
    known author (Global Semi Research -> SemiAnalysis, found 2026-07-18).
    If the sender name is already a known author, use it; if unknown,
    auto-create its author dir (the email analogue of PDF store paths, which
    create author dirs on store) so triage knows it from the next sweep on,
    and send a Telegram notice.
    """
    if scope.get("subject_type") != "author":
        return scope
    display = _sender_display_name(sender)
    if not display:
        return scope
    from scripts.authors import canonicalize_author

    display = canonicalize_author(display) or display
    subject = str(scope.get("subject") or "")
    if display.lower() == subject.lower():
        return scope
    from scripts.triage import _existing_macro_authors, _existing_semis_authors

    known = {a.lower(): a for a in _existing_macro_authors() + _existing_semis_authors()}
    if display.lower() in known:
        return {**scope, "subject": known[display.lower()]}
    # Unknown publication: add it under the category triage chose.
    import re

    safe = re.sub(r"[<>:\"/\\|?*]", " ", display)
    safe = re.sub(r"\s+", " ", safe).strip().rstrip(".")
    if not safe:
        return scope
    category = "Semis" if scope.get("corpus_type") == "semis" else "Macro"
    try:
        (DATA_DIR / category / "authors" / safe / "notes").mkdir(parents=True, exist_ok=True)
    except OSError:
        return scope
    try:
        telegram_send(
            f"📁 New research author auto-added from email: {safe} ({category}). "
            f"Triage will recognise it from the next sweep; rename/merge the "
            f"folder under data/{category}/authors/ if misfiled."
        )
    except Exception:
        pass
    return {**scope, "subject": safe}


def _extract_structured_email(
    *,
    md_path: Path,
    subject: str,
    sender: str,
    date: str,
    message_id: str,
    body_text: str,
) -> dict:
    from scripts.llm_provider import complete_json, env_config
    from scripts.parallel_ingest import (
        EXTRACT_MAX_CHARS,
        TRIAGE_MAX_CHARS,
        _build_extraction_prompt,
        _normalize_extraction,
        _normalize_triage,
        _triage_prompt,
    )
    import scripts.research_memory as research_memory

    if not body_text.strip():
        return {"structured": False, "reason": "empty body"}

    pseudo_path = Path(f"{subject or md_path.parent.name}.eml")
    # Sender identifies the publication — triage previously saw only
    # subject + body and could snap the author to the nearest known name.
    triage_text = f"Email from: {sender}\nSubject: {subject}\n\n{body_text}"
    system, triage_prompt = _triage_prompt(pseudo_path, triage_text[:TRIAGE_MAX_CHARS])
    triage = complete_json(
        triage_prompt,
        config=env_config("TRIAGE", "openai", "gpt-5-mini", timeout=180.0),
        system=system,
        max_output_tokens=2048,
    )
    triage = _normalize_triage(triage, pseudo_path)
    extraction_prompt = _build_extraction_prompt(triage, body_text[:EXTRACT_MAX_CHARS])
    extraction = complete_json(
        extraction_prompt,
        config=env_config("EXTRACTION", "openai", "gpt-5.1", timeout=300.0),
        max_output_tokens=16384,
    )
    extraction = _normalize_extraction(extraction, triage)
    meta = extraction.setdefault("metadata", {})
    meta.setdefault("title", subject or md_path.parent.name)
    meta.setdefault("source", sender or "email")
    meta.setdefault("author", sender or None)
    meta.setdefault("date", date or None)
    meta["source_type"] = "email_research"
    meta["email_message_id"] = message_id
    meta["email_source_path"] = str(md_path)
    meta["memory_scope"] = _reconcile_author_scope(_memory_scope_from_triage(triage), sender)
    extraction["email_triage"] = triage

    json_path = md_path.with_name(md_path.name + ".json")
    json_path.write_text(json.dumps(extraction, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    conn = kb.connect()
    try:
        row = research_memory.ingest_file(conn, json_path)
        conn.commit()
    finally:
        conn.close()
    return {"structured": True, "json_path": str(json_path), **row}


def _index_text_attachment(path: Path, stats: dict) -> None:
    if path.suffix.lower() not in {".md", ".txt", ".html", ".htm"}:
        return
    try:
        result = kb.index_file(
            path,
            metadata={"source": "email_attachment"},
            source_type="email_attachment",
            force=True,
        )
        if result.get("indexed"):
            stats["indexed_attachments"] += 1
    except Exception as exc:
        stats.setdefault("attachment_errors", []).append({"path": str(path), "error": str(exc)[:500]})


def _ingest_saved_message(
    msg: Message,
    *,
    dest_dir: Path,
    mailbox: str,
    uid: str,
    analyse_attachments: bool,
    extract_research: bool,
    stats: dict,
    depth: int = 0,
) -> dict:
    subject = _decode(msg.get("Subject"))
    sender = _decode(msg.get("From"))
    date = _decode(msg.get("Date"))
    message_id = msg.get("Message-ID") or uid
    body_text, body_html = _message_body(msg)
    has_attachments = _has_attachments(msg)
    if _is_low_value_email(subject, sender, body_text) and not has_attachments:
        return {
            "message_id": message_id,
            "subject": subject,
            "skipped": "non_research",
            "processed_at": datetime.now().isoformat(timespec="seconds"),
        }

    dest_dir.mkdir(parents=True, exist_ok=True)
    md_path = dest_dir / "message.md"
    body_for_storage = body_text.strip() or "(No message body extracted; processing attachments.)"
    md = (
        f"# {subject or '(no subject)'}\n\n"
        f"- Source: email\n"
        f"- From: {sender}\n"
        f"- Date: {date}\n"
        f"- Message-ID: {message_id}\n"
        f"- Mailbox: {mailbox}\n\n"
        f"{body_for_storage}\n"
    )
    md_path.write_text(md, encoding="utf-8")
    if body_html:
        (dest_dir / "message.html").write_text(body_html, encoding="utf-8")

    result = kb.index_text(
        title=subject or uid,
        text=md,
        source_type="email",
        source_uri=f"email:{message_id}",
        source_path=str(md_path),
        author=sender,
        metadata={"uid": uid, "mailbox": mailbox, "date": date, "depth": depth},
        force=True,
    )
    if result.get("indexed"):
        stats["indexed"] += 1

    structured_ok = False
    if extract_research and body_text.strip():
        try:
            extraction = _extract_structured_email(
                md_path=md_path,
                subject=subject,
                sender=sender,
                date=date,
                message_id=message_id,
                body_text=body_text,
            )
            if extraction.get("structured"):
                stats["structured_extracted"] += 1
                structured_ok = True
        except Exception as exc:
            stats["structured_failed"] += 1
            stats.setdefault("structured_errors", []).append({
                "subject": subject,
                "error": f"{type(exc).__name__}: {str(exc)[:700]}",
            })

    attachments = _save_attachments(msg, dest_dir)
    stats["attachments"] += len(attachments)
    for attachment in attachments:
        suffix = attachment.suffix.lower()
        if suffix == ".pdf":
            enqueue_job(
                "ingest_file",
                {"path": str(attachment), "notify": analyse_attachments},
                dedupe_key=f"ingest_file:{kb.file_hash(attachment)}",
            )
            stats["queued_pdfs"] += 1
        elif suffix == ".eml":
            stats["eml_attachments"] += 1
            if depth >= MAX_EML_DEPTH:
                stats.setdefault("attachment_errors", []).append({
                    "path": str(attachment),
                    "error": f"maximum .eml recursion depth {MAX_EML_DEPTH} reached",
                })
                continue
            raw = attachment.read_bytes()
            nested_msg = email.message_from_bytes(raw)
            nested_subject = _decode(nested_msg.get("Subject")) or attachment.stem
            nested_dir = dest_dir / "attached_eml" / kb.slugify(nested_subject, max_len=80)
            _ingest_saved_message(
                nested_msg,
                dest_dir=nested_dir,
                mailbox=mailbox,
                uid=f"{uid}:{kb.file_hash(attachment)[:16]}",
                analyse_attachments=analyse_attachments,
                extract_research=extract_research,
                stats=stats,
                depth=depth + 1,
            )
        else:
            _index_text_attachment(attachment, stats)

    return {
        "key": kb.text_hash(message_id)[:20],
        "message_id": message_id,
        "subject": subject,
        "sender": sender,
        "date": date,
        "md_path": str(md_path),
        "source_uri": f"email:{message_id}",
        "structured": structured_ok,
        "processed_at": datetime.now().isoformat(timespec="seconds"),
    }


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


def _gmail_client_secret_path() -> Path:
    value = os.environ.get("GMAIL_OAUTH_CLIENT_SECRET") or os.environ.get("GMAIL_CLIENT_SECRET_PATH")
    if not value:
        value = str(PROJECT_ROOT / "config" / "gmail_oauth_client_secret.json")
    return Path(value).expanduser().resolve()


def _gmail_token_path() -> Path:
    value = os.environ.get("GMAIL_OAUTH_TOKEN") or os.environ.get("GMAIL_TOKEN_PATH")
    if not value:
        value = str(SECRETS_DIR / "gmail_token.json")
    return Path(value).expanduser().resolve()


def gmail_authorize(
    *,
    client_secret_path: str | Path | None = None,
    token_path: str | Path | None = None,
    port: int = 0,
) -> dict:
    """Run the installed-app OAuth flow once and save a refreshable token."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise RuntimeError(
            "Missing Gmail OAuth dependency. Run: pip install -r requirements.txt"
        ) from exc

    client_secret = Path(client_secret_path).expanduser().resolve() if client_secret_path else _gmail_client_secret_path()
    token = Path(token_path).expanduser().resolve() if token_path else _gmail_token_path()
    if not client_secret.exists():
        raise FileNotFoundError(
            f"Gmail OAuth client secret JSON not found: {client_secret}. "
            "Download a Desktop OAuth client JSON from Google Cloud and place it there, "
            "or set GMAIL_OAUTH_CLIENT_SECRET."
        )
    token.parent.mkdir(parents=True, exist_ok=True)
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), GMAIL_SCOPES)
    creds = flow.run_local_server(port=port)
    token.write_text(creds.to_json(), encoding="utf-8")
    return {
        "client_secret_path": str(client_secret),
        "token_path": str(token),
        "scopes": GMAIL_SCOPES,
    }


def _gmail_credentials():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:
        raise RuntimeError(
            "Missing Gmail OAuth dependency. Run: pip install -r requirements.txt"
        ) from exc

    token = _gmail_token_path()
    client_secret = _gmail_client_secret_path()
    if not token.exists():
        raise RuntimeError(
            f"Gmail OAuth token not found: {token}. "
            "Run: python main.py gmail-auth"
        )
    creds = Credentials.from_authorized_user_file(str(token), GMAIL_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text(creds.to_json(), encoding="utf-8")
    if not creds or not creds.valid:
        raise RuntimeError("Gmail OAuth token is invalid. Run: python main.py gmail-auth")
    if not client_secret.exists():
        raise FileNotFoundError(
            f"Gmail OAuth client secret JSON not found: {client_secret}. "
            "Set GMAIL_OAUTH_CLIENT_SECRET or place the JSON there."
        )
    return creds


def _gmail_service():
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError("Missing google-api-python-client. Run: pip install -r requirements.txt") from exc
    return build("gmail", "v1", credentials=_gmail_credentials(), cache_discovery=False)


def _decode_gmail_raw(raw: str) -> bytes:
    padded = raw + ("=" * (-len(raw) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _gmail_query() -> str:
    return os.environ.get("GMAIL_QUERY") or os.environ.get("RESEARCH_GMAIL_QUERY") or "in:inbox"


def _iter_gmail_api_messages(limit: int) -> tuple[str, list[tuple[str, bytes]]]:
    service = _gmail_service()
    query = _gmail_query()
    result = service.users().messages().list(
        userId="me",
        q=query,
        maxResults=max(1, int(limit or 50)),
    ).execute()
    messages = result.get("messages") or []
    out: list[tuple[str, bytes]] = []
    for item in messages:
        message_id = item.get("id")
        if not message_id:
            continue
        fetched = service.users().messages().get(
            userId="me",
            id=message_id,
            format="raw",
        ).execute()
        raw = fetched.get("raw")
        if raw:
            out.append((f"gmail:{message_id}", _decode_gmail_raw(raw)))
    return f"gmail:{query}", out


def _iter_imap_messages(limit: int) -> tuple[str, list[tuple[str, bytes]]]:
    client, mailbox = _connect()
    out: list[tuple[str, bytes]] = []
    try:
        status, data = client.uid("SEARCH", None, "ALL")
        if status != "OK":
            raise RuntimeError(f"IMAP search failed: {status}")
        uids = data[0].split()[-limit:]
        for uid_b in uids:
            uid = uid_b.decode()
            status, msg_data = client.uid("FETCH", uid, "(RFC822)")
            if status != "OK" or not msg_data:
                continue
            out.append((f"imap:{mailbox}:{uid}", msg_data[0][1]))
    finally:
        try:
            client.logout()
        except Exception:
            pass
    return mailbox, out


def email_sweep(
    notify: bool = False,
    limit: int = 50,
    analyse_attachments: bool = False,
    extract_research: bool = True,
    combined: bool = False,
) -> dict:
    EMAIL_DIR.mkdir(parents=True, exist_ok=True)
    state = _load_state()
    processed = state.setdefault("processed", {})
    stats = {
        "seen": 0,
        "new": 0,
        "indexed": 0,
        "attachments": 0,
        "queued_pdfs": 0,
        "eml_attachments": 0,
        "indexed_attachments": 0,
        "structured_extracted": 0,
        "structured_failed": 0,
        "provider": _provider(),
    }
    if stats["provider"] in {"gmail", "gmail_api", "gmail-api"}:
        mailbox, messages = _iter_gmail_api_messages(limit)
    else:
        mailbox, messages = _iter_imap_messages(limit)

    digest_items: list[dict] = []
    for uid, raw in messages:
        stats["seen"] += 1
        if uid in processed:
            continue
        msg = email.message_from_bytes(raw)
        subject = _decode(msg.get("Subject"))
        day = datetime.now().strftime("%Y-%m-%d")
        dest_dir = EMAIL_DIR / day / kb.slugify(subject or uid, max_len=80)
        record = _ingest_saved_message(
            msg,
            dest_dir=dest_dir,
            mailbox=mailbox,
            uid=uid,
            analyse_attachments=analyse_attachments,
            extract_research=extract_research,
            stats=stats,
        )
        processed[uid] = record
        stats["new"] += 1
        # Low-value/non-research messages are stored as a skip marker with no
        # key/md_path — keep them out of the analyst-facing digest list.
        if record.get("key") and not record.get("skipped"):
            digest_items.append(record)
        _save_state(state)

    digest_items = [dict(it, rank=i) for i, it in enumerate(digest_items, start=1)]
    _save_latest_email_digest(digest_items)
    stats["digest_items"] = len(digest_items)

    if combined:
        # Submit to the daily coordinator instead of sending; the merged
        # email+inbox message goes out once both sweeps conclude.
        from scripts.combined_digest import submit_part
        submit_part("email", _email_section_text(digest_items, stats))
    elif notify and digest_items:
        telegram_send_markdownish_html(_format_email_digest(digest_items, stats))
    elif notify and stats["new"]:
        telegram_send(
            f"Email sweep: {stats['new']} new message(s), but none were research "
            f"items (filtered as low-value or attachment-only)."
        )
    return stats


def _email_section_text(items: list[dict], stats: dict) -> str:
    """Email-sweep section for the combined digest, covering the empty cases so
    the merged message always has an email section."""
    if items:
        return _format_email_digest(items, stats)
    if stats.get("new"):
        return ("**Research email sweep — 0 new item(s)**\n"
                f"({stats['new']} new message(s), none were research items.)")
    return "**Research email sweep — 0 new item(s)**\n(no new research emails today)"


def _format_email_digest(items: list[dict], stats: dict) -> str:
    lines = [f"**Research email sweep — {len(items)} new item(s)**", ""]
    for it in items:
        subject = (it.get("subject") or "(no subject)").strip()
        sender = (it.get("sender") or "").strip()
        flag = " · structured" if it.get("structured") else ""
        lines.append(f"{it['rank']}. **{subject}**")
        meta = sender + flag if sender else flag.lstrip(" ·")
        if meta:
            lines.append(f"   {meta}")
        lines.append(f"   analyse: /email_{it['rank']}")
        lines.append("")
    if stats.get("queued_pdfs"):
        lines.append(f"({stats['queued_pdfs']} PDF attachment(s) queued for ingestion.)")
    return "\n".join(lines).strip()


def _save_latest_email_digest(items: list[dict]) -> None:
    path = _latest_digest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"), "items": items}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _load_latest_email_digest() -> dict:
    path = _latest_digest_path()
    if not path.exists():
        return {"items": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"items": []}


def get_email(key: str) -> dict | None:
    for it in _load_latest_email_digest().get("items", []):
        if it.get("key") == key:
            return it
    return None


def get_email_by_rank(rank: int) -> dict | None:
    for it in _load_latest_email_digest().get("items", []):
        try:
            if int(it.get("rank", 0)) == int(rank):
                return it
        except Exception:
            continue
    return None


def analyse_email(key: str, notify: bool = True) -> dict:
    item = get_email(key)
    if not item:
        raise FileNotFoundError(f"Email not found for key {key}")
    md_path = Path(item.get("md_path", ""))
    body = ""
    if md_path.exists():
        body = md_path.read_text(encoding="utf-8", errors="replace")
    from scripts.analyst import email_readthrough

    analysis = email_readthrough(
        subject=item.get("subject", ""),
        sender=item.get("sender", ""),
        body=body,
    )
    analysis_path = EMAIL_DIR / f"{datetime.now().strftime('%Y-%m-%d_%H%M%S')}_{key}_analysis.md"
    analysis_path.write_text("# Email Analysis\n\n" + analysis + "\n", encoding="utf-8")
    kb.index_text(
        title=f"Email Analysis - {item.get('subject', key)[:120]}",
        text=analysis,
        source_type="email",
        source_uri=f"email-analysis:{key}:{analysis_path.name}",
        source_path=str(analysis_path),
        metadata={"email_key": key, "subject": item.get("subject")},
        force=True,
    )
    if notify:
        telegram_send_markdownish_html(analysis)
    return {"key": key, "subject": item.get("subject"), "analysis_path": str(analysis_path)}
