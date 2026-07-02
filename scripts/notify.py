"""Small Telegram notification helper used by non-bot workers."""

from __future__ import annotations

import os
import json
import html
import logging
import re
import time

import requests

log = logging.getLogger("notify")

# Telegram hard limit is 4096 chars per message; stay under it.
MAX_SEND_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0
MAX_RETRY_AFTER_SECONDS = 60.0


def chunks(text: str, size: int = 3800) -> list[str]:
    if len(text) <= size:
        return [text]
    out, buf = [], ""
    for para in text.split("\n\n"):
        if len(para) > size:
            if buf:
                out.append(buf)
                buf = ""
            for i in range(0, len(para), size):
                out.append(para[i:i + size])
            continue
        if len(buf) + len(para) + 2 > size:
            if buf:
                out.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        out.append(buf)
    return out


def telegram_html(text: str) -> str:
    """Render a small Markdown-ish subset as safe Telegram HTML.

    Supported:
    - **bold spans**
    - # / ## headings, rendered as bold lines
    """
    out = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            out.append(f"<b>{html.escape(heading.group(2).strip(), quote=False)}</b>")
            continue

        parts = line.split("**")
        if len(parts) % 2 == 0:
            out.append(html.escape(line, quote=False))
            continue
        rendered = []
        for i, part in enumerate(parts):
            escaped = html.escape(part, quote=False)
            if i % 2 == 1 and part.strip():
                rendered.append(f"<b>{escaped}</b>")
            else:
                rendered.append(escaped)
        out.append("".join(rendered))
    return "\n".join(out)


def _retry_after_seconds(resp: requests.Response) -> float:
    """Extract Telegram's retry_after hint from a 429 response."""
    try:
        body = resp.json()
        value = float(body.get("parameters", {}).get("retry_after", 0))
        if value > 0:
            return min(value, MAX_RETRY_AFTER_SECONDS)
    except Exception:
        pass
    try:
        value = float(resp.headers.get("Retry-After", 0))
        if value > 0:
            return min(value, MAX_RETRY_AFTER_SECONDS)
    except Exception:
        pass
    return RETRY_BACKOFF_SECONDS


def _post_message(token: str, data: dict) -> bool:
    """POST one sendMessage call; retry 429/5xx/network errors. True on delivered."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    last_detail = ""
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            resp = requests.post(url, data=data, timeout=15)
        except Exception as e:
            last_detail = f"network error: {e}"
            if attempt < MAX_SEND_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue
        if resp.status_code == 200:
            return True
        if resp.status_code == 429:
            wait = _retry_after_seconds(resp)
            last_detail = f"429 rate limited (retry_after={wait:.0f}s)"
            log.warning("telegram sendMessage rate limited; waiting %.0fs (attempt %d/%d)",
                        wait, attempt, MAX_SEND_ATTEMPTS)
            if attempt < MAX_SEND_ATTEMPTS:
                time.sleep(wait)
            continue
        # Response bodies for 4xx are short JSON error descriptions (no secrets).
        try:
            desc = resp.json().get("description", "")
        except Exception:
            desc = resp.text[:200]
        last_detail = f"HTTP {resp.status_code}: {desc}"
        if 500 <= resp.status_code < 600 and attempt < MAX_SEND_ATTEMPTS:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue
        break  # 4xx other than 429 will not succeed on retry
    log.error("telegram sendMessage failed after %d attempt(s): %s "
              "(chat_id=%s, %d chars)", attempt, last_detail,
              data.get("chat_id"), len(str(data.get("text", ""))))
    return False


def telegram_send(
    text: str,
    parse_mode: str | None = None,
    disable_web_page_preview: bool | None = None,
) -> bool:
    """Send text to every allowed user. Returns True only if every chunk
    was delivered to every user; failures are logged, never raised."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    raw_ids = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if not token or not raw_ids:
        log.warning("telegram_send skipped: TELEGRAM_BOT_TOKEN/ALLOWED_USER_IDS not set")
        return False
    user_ids = [int(x) for x in raw_ids.split(",") if x.strip()]
    ok = True
    for piece in chunks(text):
        for uid in user_ids:
            data = {"chat_id": uid, "text": piece}
            if parse_mode:
                data["parse_mode"] = parse_mode
            if disable_web_page_preview is not None:
                data["disable_web_page_preview"] = "true" if disable_web_page_preview else "false"
            if not _post_message(token, data):
                ok = False
    return ok


def telegram_send_markdownish_html(
    text: str,
    disable_web_page_preview: bool | None = None,
) -> bool:
    ok = True
    for piece in chunks(text):
        if not telegram_send(
            telegram_html(piece),
            parse_mode="HTML",
            disable_web_page_preview=disable_web_page_preview,
        ):
            ok = False
    return ok


def telegram_send_with_buttons(
    text: str,
    buttons: list[dict],
    parse_mode: str | None = None,
    disable_web_page_preview: bool | None = None,
) -> bool:
    """Send one Telegram message with an inline keyboard.

    buttons: [{"text": "Analyse 1", "callback_data": "ha:<key>"}] or
             [{"text": "Link 1", "url": "https://..."}]
    Returns True only if delivered to every user.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    raw_ids = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if not token or not raw_ids:
        log.warning("telegram_send_with_buttons skipped: token/user ids not set")
        return False
    user_ids = [int(x) for x in raw_ids.split(",") if x.strip()]
    rows = []
    for button in buttons:
        if isinstance(button, list):
            rows.append(button)
        else:
            rows.append([button])
    reply_markup = json.dumps({"inline_keyboard": rows}, ensure_ascii=False)
    body = text if len(text) <= 3900 else text[:3900] + "\n\n... truncated"
    ok = True
    for uid in user_ids:
        data = {
            "chat_id": uid,
            "text": body,
            "reply_markup": reply_markup,
            **({"parse_mode": parse_mode} if parse_mode else {}),
            **(
                {"disable_web_page_preview": "true" if disable_web_page_preview else "false"}
                if disable_web_page_preview is not None
                else {}
            ),
        }
        if not _post_message(token, data):
            ok = False
    return ok
