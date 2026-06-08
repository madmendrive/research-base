"""Small Telegram notification helper used by non-bot workers."""

from __future__ import annotations

import os
import json
import html
import re

import requests


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


def telegram_send(
    text: str,
    parse_mode: str | None = None,
    disable_web_page_preview: bool | None = None,
) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    raw_ids = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if not token or not raw_ids:
        return
    user_ids = [int(x) for x in raw_ids.split(",") if x.strip()]
    for piece in chunks(text):
        for uid in user_ids:
            try:
                data = {"chat_id": uid, "text": piece}
                if parse_mode:
                    data["parse_mode"] = parse_mode
                if disable_web_page_preview is not None:
                    data["disable_web_page_preview"] = "true" if disable_web_page_preview else "false"
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data=data,
                    timeout=15,
                )
            except Exception:
                pass


def telegram_send_markdownish_html(
    text: str,
    disable_web_page_preview: bool | None = None,
) -> None:
    for piece in chunks(text):
        telegram_send(
            telegram_html(piece),
            parse_mode="HTML",
            disable_web_page_preview=disable_web_page_preview,
        )


def telegram_send_with_buttons(
    text: str,
    buttons: list[dict],
    parse_mode: str | None = None,
    disable_web_page_preview: bool | None = None,
) -> None:
    """Send one Telegram message with an inline keyboard.

    buttons: [{"text": "Analyse 1", "callback_data": "ha:<key>"}] or
             [{"text": "Link 1", "url": "https://..."}]
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    raw_ids = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if not token or not raw_ids:
        return
    user_ids = [int(x) for x in raw_ids.split(",") if x.strip()]
    rows = []
    for button in buttons:
        if isinstance(button, list):
            rows.append(button)
        else:
            rows.append([button])
    reply_markup = json.dumps({"inline_keyboard": rows}, ensure_ascii=False)
    body = text if len(text) <= 3900 else text[:3900] + "\n\n... truncated"
    for uid in user_ids:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={
                    "chat_id": uid,
                    "text": body,
                    "reply_markup": reply_markup,
                    **({"parse_mode": parse_mode} if parse_mode else {}),
                    **(
                        {"disable_web_page_preview": "true" if disable_web_page_preview else "false"}
                        if disable_web_page_preview is not None
                        else {}
                    ),
                },
                timeout=15,
            )
        except Exception:
            pass
