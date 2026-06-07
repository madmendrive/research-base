"""Telegram bot — single-user gateway to the research pipeline.

Default UX: drop a PDF into the chat. The bot auto-classifies, stores it under
the right entity, and runs cross-cutting analysis vs every other ticker/theme
it touches.

Manual overrides:
  /research TICKER  — force storage as sellside research for TICKER
  /macro AUTHOR     — force storage as macro research by AUTHOR
  /thematic THEME   — force storage as thematic research for THEME

Other:
  /start, /help, /status, /cancel

Env vars (in .env):
  TELEGRAM_BOT_TOKEN          token from @BotFather
  TELEGRAM_ALLOWED_USER_IDS   comma-separated Telegram user IDs
"""

import truststore; truststore.inject_into_ssl()

import os
import json
import logging
import tempfile
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Telegram Bot API caps incoming file downloads at 20 MB.
TELEGRAM_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

PROJECT_ROOT = Path(__file__).parent
COMPANIES_PATH = PROJECT_ROOT / "config" / "companies.json"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _allowed_ids() -> set[int]:
    raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if not raw:
        return set()
    return {int(x) for x in raw.split(",") if x.strip()}


ALLOWED = _allowed_ids()


def _is_allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ALLOWED)


async def _deny(update: Update) -> None:
    log.warning("denied: user_id=%s username=%s",
                update.effective_user.id if update.effective_user else None,
                update.effective_user.username if update.effective_user else None)
    await update.message.reply_text("Not authorised.")


# ---------------------------------------------------------------------------
# Per-user override state — {user_id: {"mode": "research"|"macro"|"thematic", "arg": "..."}}
# ---------------------------------------------------------------------------

PENDING: dict[int, dict] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_companies() -> dict:
    with open(COMPANIES_PATH, encoding="utf-8") as f:
        return json.load(f)


def _chunk(text: str, size: int = 3800) -> list[str]:
    """Split text on paragraph boundaries to fit Telegram's 4096-char message limit."""
    if len(text) <= size:
        return [text]
    chunks, buf = [], ""
    for para in text.split("\n\n"):
        if len(para) > size:
            # Single huge paragraph — hard-split
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(para), size):
                chunks.append(para[i:i + size])
            continue
        if len(buf) + len(para) + 2 > size:
            if buf:
                chunks.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        chunks.append(buf)
    return chunks


async def _send_long(update: Update, text: str) -> None:
    for piece in _chunk(text):
        await update.message.reply_text(piece)


async def _run_blocking(fn, *args, **kwargs):
    """Run a blocking function (Claude API calls, store_*) off the event loop."""
    import asyncio
    loop = asyncio.get_running_loop()
    if kwargs:
        from functools import partial
        return await loop.run_in_executor(None, partial(fn, *args, **kwargs))
    return await loop.run_in_executor(None, fn, *args)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "Research pipeline bot.\n\n"
    "Default: drop a PDF and I'll figure out what it is, store it, and "
    "cross-read it against every ticker/theme it touches.\n\n"
    "Manual overrides:\n"
    "/research TICKER — force as sellside research\n"
    "/macro AUTHOR    — force as macro research\n"
    "/thematic THEME  — force as thematic research\n\n"
    "/status — show pending override\n"
    "/cancel — clear pending override\n"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    await update.message.reply_text(HELP_TEXT)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    await update.message.reply_text(HELP_TEXT)


async def cmd_research(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    if not context.args:
        await update.message.reply_text("Usage: /research TICKER")
        return
    ticker = context.args[0].strip()
    companies = _load_companies()
    if ticker not in companies:
        await update.message.reply_text(f"Unknown ticker '{ticker}'.")
        return
    PENDING[update.effective_user.id] = {"mode": "research", "arg": ticker}
    await update.message.reply_text(
        f"Next PDF will be forced as sellside research for {ticker} ({companies[ticker].get('name', '')})."
    )


async def cmd_macro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    if not context.args:
        await update.message.reply_text("Usage: /macro AUTHOR")
        return
    author = " ".join(context.args).strip().strip('"').strip("'")
    PENDING[update.effective_user.id] = {"mode": "macro", "arg": author}
    await update.message.reply_text(f"Next PDF will be forced as macro research by '{author}'.")


async def cmd_thematic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    if not context.args:
        await update.message.reply_text("Usage: /thematic THEME")
        return
    theme = " ".join(context.args).strip().strip('"').strip("'")
    PENDING[update.effective_user.id] = {"mode": "thematic", "arg": theme}
    await update.message.reply_text(f"Next PDF will be forced as thematic research for '{theme}'.")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    PENDING.pop(update.effective_user.id, None)
    await update.message.reply_text("Override cleared.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    state = PENDING.get(update.effective_user.id)
    if not state:
        await update.message.reply_text("No override. PDFs will be auto-classified.")
        return
    await update.message.reply_text(f"Override: next PDF will be forced as {state['mode']} → {state['arg']}")


# ---------------------------------------------------------------------------
# Document handler — the main flow
# ---------------------------------------------------------------------------

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)

    doc = update.message.document
    if not doc:
        return
    if not (doc.file_name or "").lower().endswith(".pdf"):
        await update.message.reply_text("Only PDFs supported for now.")
        return

    if doc.file_size and doc.file_size > TELEGRAM_MAX_DOWNLOAD_BYTES:
        mb = doc.file_size / (1024 * 1024)
        await update.message.reply_text(
            f"File is {mb:.1f} MB — Telegram caps bot downloads at 20 MB.\n\n"
            f"Workaround: save the PDF to\n"
            f"  C:\\Users\\Owner\\research-pipeline\\downloads\\\n"
            f"and the folder sweeper (when set up) will ingest it.\n\n"
            f"Or compress the PDF first (Acrobat / online tools)."
        )
        return

    user_id = update.effective_user.id
    override = PENDING.pop(user_id, None)

    await update.message.chat.send_action(ChatAction.TYPING)
    await update.message.reply_text(f"Downloading {doc.file_name}…")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / doc.file_name
        try:
            tg_file = await doc.get_file()
            await tg_file.download_to_drive(str(tmp_path))
        except BadRequest as e:
            await update.message.reply_text(f"Telegram refused the download: {e}")
            return

        try:
            if override:
                await _process_with_override(update, override, tmp_path)
            else:
                await _process_auto(update, tmp_path)
        except Exception as e:
            log.exception("ingest failed")
            await update.message.reply_text(f"Ingestion failed: {e}")


async def _process_auto(update: Update, pdf_path: Path) -> None:
    """Default drop-file-and-go flow: triage → store → cross-cut."""
    from scripts.bot_pipeline import (
        _store_primary,
        _derive_secondaries,
        _cross_analyse_ticker,
        _cross_analyse_theme,
    )
    from scripts.triage import triage_document, format_triage_for_user

    # 1. Triage
    await update.message.reply_text("Triaging…")
    await update.message.chat.send_action(ChatAction.TYPING)
    triage = await _run_blocking(triage_document, pdf_path)
    await update.message.reply_text(format_triage_for_user(triage))

    # 2. Store primary
    await update.message.reply_text(
        f"Storing as primary ({triage['primary_type']} → {triage['primary_subject']})…"
    )
    await update.message.chat.send_action(ChatAction.TYPING)
    primary = await _run_blocking(_store_primary, triage, pdf_path)

    # 3. Cross-cuts
    secondaries = _derive_secondaries(triage)
    if secondaries:
        await update.message.reply_text(
            f"Cross-reading against {len(secondaries)} other entit{'y' if len(secondaries) == 1 else 'ies'}…"
        )

    # 4. Send primary report first
    await update.message.reply_text(f"━━━ PRIMARY: {triage['primary_subject']} ━━━")
    await _send_long(update, primary)

    # 5. Send each secondary report
    for label, kind, target in secondaries:
        await update.message.chat.send_action(ChatAction.TYPING)
        await update.message.reply_text(f"━━━ {label} ━━━")
        try:
            if kind == "ticker":
                rpt = await _run_blocking(_cross_analyse_ticker, target, pdf_path)
            else:
                rpt = await _run_blocking(_cross_analyse_theme, target, pdf_path)
            await _send_long(update, rpt)
        except Exception as e:
            log.exception("cross-analysis failed for %s", target)
            await update.message.reply_text(f"Failed: {e}")

    await update.message.reply_text("Done.")


async def _process_with_override(update: Update, override: dict, pdf_path: Path) -> None:
    """Forced storage as a specific type — skip triage, just store."""
    from scripts.bot_pipeline import (
        _store_primary_research,
        _store_primary_macro,
        _store_primary_thematic,
    )

    mode, arg = override["mode"], override["arg"]
    await update.message.reply_text(f"Forced mode: {mode} → {arg}. Ingesting…")
    await update.message.chat.send_action(ChatAction.TYPING)

    if mode == "research":
        rpt = await _run_blocking(_store_primary_research, arg, pdf_path)
    elif mode == "macro":
        rpt = await _run_blocking(_store_primary_macro, arg, pdf_path)
    elif mode == "thematic":
        rpt = await _run_blocking(_store_primary_thematic, arg, pdf_path)
    else:
        rpt = f"Unknown override mode {mode!r}."

    await _send_long(update, rpt)
    await update.message.reply_text("Done.")


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    await update.message.reply_text("Unknown command. /help for the list.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Last-resort handler — log and tell the user something broke."""
    log.exception("Unhandled exception in handler", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                f"Something broke: {type(context.error).__name__}: {context.error}"
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set in .env")
    if not ALLOWED:
        raise SystemExit("TELEGRAM_ALLOWED_USER_IDS not set in .env")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("research", cmd_research))
    app.add_handler(CommandHandler("macro", cmd_macro))
    app.add_handler(CommandHandler("thematic", cmd_thematic))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.COMMAND, handle_unknown))
    app.add_error_handler(on_error)

    log.info("bot starting; allowed user IDs: %s", sorted(ALLOWED))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
