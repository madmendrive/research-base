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
import re
from logging.handlers import RotatingFileHandler
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Telegram Bot API caps incoming file downloads at 20 MB.
TELEGRAM_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

PROJECT_ROOT = Path(__file__).parent
COMPANIES_PATH = PROJECT_ROOT / "config" / "companies.json"
TELEGRAM_UPLOAD_DIR = PROJECT_ROOT / "data" / "_telegram_uploads"
BOT_RUNTIME_LOG = PROJECT_ROOT / "data" / "_bot_runtime.log"

BOT_RUNTIME_LOG.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(BOT_RUNTIME_LOG, maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
    ],
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
    # effective_message covers edited_message updates too (message is None there).
    if update.effective_message:
        await update.effective_message.reply_text("Not authorised.")


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


def _tech_brief_status_text() -> str:
    from scripts import kb
    from scripts.headlines import LATEST_DIGEST_PATH

    lines = ["**Tech Brief status**"]
    if LATEST_DIGEST_PATH.exists():
        try:
            latest = json.loads(LATEST_DIGEST_PATH.read_text(encoding="utf-8"))
            generated_at = latest.get("generated_at") or "unknown"
            items = latest.get("items") or []
            lines.append(f"- The latest saved Tech Brief was generated at {generated_at}.")
            lines.append(f"- The latest digest contains {len(items)} headline item(s).")
        except Exception as e:
            lines.append(f"- I found the latest digest file, but could not read it: {type(e).__name__}: {e}.")
    else:
        lines.append("- I do not see a saved Tech Brief digest yet.")

    conn = kb.connect()
    try:
        rows = conn.execute(
            """
            SELECT id, status, attempts, created_at, started_at, finished_at, last_error, payload_json
            FROM jobs
            WHERE kind = 'headline_sweep'
            ORDER BY id DESC
            LIMIT 5
            """
        ).fetchall()
        if rows:
            lines.append("")
            lines.append("**Recent headline sweep jobs**")
            for row in rows:
                payload = json.loads(row["payload_json"] or "{}")
                detail = (
                    f"- Job #{row['id']} is {row['status']}."
                    f" It was created at {row['created_at']}"
                    f" and finished at {row['finished_at'] or 'not finished'}."
                )
                if payload.get("window_hours"):
                    detail += f" The lookback window was {payload.get('window_hours')} hour(s)."
                if row["last_error"]:
                    detail += f" Last error: {row['last_error']}."
                lines.append(detail)
        else:
            lines.append("")
            lines.append("- I do not see any headline sweep jobs in the queue history.")
    finally:
        conn.close()

    state_path = PROJECT_ROOT / "data" / "_kb" / "heartbeat_state.json"
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            headline_slots = sorted(k for k in state if k.startswith("headline:"))
            if headline_slots:
                lines.append("")
                lines.append("**Scheduler headline slots**")
                for key in headline_slots[-6:]:
                    lines.append(f"- {key} was marked at {state[key]}.")
        except Exception:
            pass
    return "\n".join(lines)


# Genuine "is the brief/sweep machinery working?" queries take the deterministic
# status fast-path. Analysis requests that merely mention the word "headline"
# (e.g. "analyse this headline: ...") must fall through to the analyst.
_BRIEF_TOPIC_RE = re.compile(r"\b(tech brief|headlines?)\b", re.IGNORECASE)
_ANALYSIS_INTENT_RE = re.compile(
    r"\b(analy[sz]e|analy[sz]is|summari[sz]e|thoughts?|implications?|impact|"
    r"read[\s-]?through|interpret|make of|view on|take on|read on|mean(s|ing)?\s+for|"
    r"comment|compare|chronolog\w*|incremental|history|evolution|"
    r"list\s+(?:all|the|every)|in\s+the\s+last\s+\d|over\s+the\s+(?:last|past))\b",
    re.IGNORECASE,
)
# Operational terms only. Generic words (last/latest/recent/did/when/why)
# were removed 2026-07-23: they matched research phrasing like "in the last
# 2 months" and sent substantive questions to the status dump. A status
# question that misses this fast-path still gets answered — the agentic
# analyst has the pipeline_status tool — but a research question that
# false-positives here gets a useless status dump.
_STATUS_INTENT_RE = re.compile(
    r"\b(status|ran|run(ning)?|sweep|arrive[ds]?|generate[ds]?|schedule[ds]?|"
    r"missed|pending|queue[ds]?|finish(ed)?|complete[ds]?|fail(ed|ing)?)\b",
    re.IGNORECASE,
)


def _is_tech_brief_status_query(text: str) -> bool:
    """True only for genuine Tech Brief / headline-sweep *status* questions.

    Returns False for analysis requests that merely contain the word "headline"
    so they reach the analyst instead of the status fast-path.
    """
    if not _BRIEF_TOPIC_RE.search(text):
        return False
    if _ANALYSIS_INTENT_RE.search(text):
        return False
    # Bare topic ("headlines", "tech brief?") or an explicit operational query.
    if len(text.split()) <= 3:
        return True
    return bool(_STATUS_INTENT_RE.search(text))


def _chunk(text: str, size: int = 3800) -> list[str]:
    """Split text to fit Telegram's 4096-char message limit (word-boundary aware)."""
    from scripts.notify import chunks

    return chunks(text, size=size)


async def _send_long(update: Update, text: str, parse_mode: str | None = None) -> None:
    for piece in _chunk(text):
        await update.message.reply_text(piece, parse_mode=parse_mode)


async def _send_long_html(update: Update, text: str) -> None:
    from scripts.notify import telegram_html

    for piece in _chunk(text):
        await update.effective_message.reply_text(telegram_html(piece), parse_mode="HTML")


async def _run_blocking(fn, *args, timeout_seconds: float | None = None, **kwargs):
    """Run a blocking function (Claude API calls, store_*) off the event loop."""
    import asyncio
    loop = asyncio.get_running_loop()
    if kwargs:
        from functools import partial
        future = loop.run_in_executor(None, partial(fn, *args, **kwargs))
    else:
        future = loop.run_in_executor(None, fn, *args)
    if timeout_seconds:
        return await asyncio.wait_for(future, timeout=timeout_seconds)
    return await future


async def _queue_analyst_question(update: Update, question: str, source: str) -> None:
    """Hand the question to the job worker — no deadline; the worker pushes
    the full answer to Telegram whenever the synthesis finishes."""
    import time as _time

    from scripts.jobs import enqueue_job

    # Enqueue first: this is a local DB write that can't time out. The cosmetic
    # typing indicator + ack below talk to Telegram and occasionally time out;
    # a failure there must never drop the user's question. The worker delivers
    # the full answer regardless of whether this ack lands.
    job_id = enqueue_job(
        "analyst_question",
        {"question": question, "user_id": update.effective_user.id},
        # Unique key per ask so repeating a question later still runs.
        dedupe_key=f"analyst_question:{update.effective_user.id}:{int(_time.time() * 1000)}",
        max_attempts=1,
    )
    log.info("%s analyst question queued user_id=%s chars=%s job=%s",
             source, update.effective_user.id, len(question), job_id)
    try:
        await update.effective_message.chat.send_action(ChatAction.TYPING)
        await update.effective_message.reply_text(
            "Queued for the analyst. I am checking the local KB and structured research "
            "memory and will send the full answer here when the synthesis finishes — "
            "detailed questions can take several minutes."
        )
    except Exception as e:
        log.warning("analyst ack to user failed (job %s still queued): %s", job_id, e)



# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "Research pipeline bot.\n\n"
    "Default: drop a PDF and I'll queue it for the research worker to classify, store, and index.\n"
    "To paste a text snippet: start the message with 'ingest this [note] [by AUTHOR]', then paste the text.\n\n"
    "Manual overrides:\n"
    "/research TICKER - force as sellside research\n"
    "/macro AUTHOR    - force as macro research\n"
    "/thematic THEME  - force as thematic research\n\n"
    "Knowledge base:\n"
    "/ask QUESTION     - analyst-style investment answer\n"
    "/search QUERY     - diagnostic KB matches\n"
    "/note TARGET TEXT - store your own note\n"
    "/download TICKER  - queue IR/regulatory download\n"
    "/pending          - show held low-confidence files\n"
    "/headline_#       - analyse one item from the latest Tech Brief\n"
    "/email_#          - analyse one item from the latest email sweep\n"
    "/inbox_#          - analyse one item from the latest inbox scan digest\n\n"
    "Research memory:\n"
    "/memory SUBJECT   - structured views/estimates snapshot\n"
    "/mapstatus        - structured memory coverage\n\n"
    "Learning:\n"
    "/teach LESSON     - save a durable analyst rule or preference\n"
    "/feedback TEXT    - critique the latest answer so future answers improve\n"
    "/lessons          - show active analyst lessons\n\n"
    "/status - show pending override\n"
    "/cancel - clear pending override\n"
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
# Knowledge-base commands
# ---------------------------------------------------------------------------

async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    question = " ".join(context.args).strip()
    if not question:
        await update.message.reply_text("Usage: /ask QUESTION")
        return
    await _queue_analyst_question(update, question, source="/ask")


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Usage: /search QUERY")
        return
    from scripts.kb import format_results, search

    results = await _run_blocking(search, query)
    await _send_long(update, format_results(results))


async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /note TICKER_OR_THEME note text...")
        return
    target = context.args[0].strip()
    text = " ".join(context.args[1:]).strip()
    from scripts.jobs import enqueue_job

    job_id = enqueue_job(
        "store_note",
        {
            "target": target,
            "text": text,
            "author": update.effective_user.username or str(update.effective_user.id),
            "notify": True,
        },
    )
    await update.message.reply_text(f"Queued note for {target}. Job #{job_id}.")


async def cmd_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    if not context.args:
        await update.message.reply_text("Usage: /download TICKER")
        return
    ticker = context.args[0].strip()
    companies = _load_companies()
    if ticker not in companies:
        await update.message.reply_text(f"Unknown ticker '{ticker}'.")
        return
    from scripts.jobs import enqueue_job

    job_id = enqueue_job(
        "download_materials",
        {"ticker": ticker, "since": 2020, "limit": 200, "notify": True},
        dedupe_key=f"download_materials:{ticker}",
    )
    await update.message.reply_text(f"Queued download-materials for {ticker}. Job #{job_id}.")


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    from scripts.ops import list_pending

    pending = list_pending(limit=10)
    if not pending:
        await update.message.reply_text("No pending low-confidence files.")
        return
    for item in pending:
        token = item["token"]
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Confirm", callback_data=f"pc:{token}"),
                InlineKeyboardButton("Reclassify", callback_data=f"pr:{token}"),
                InlineKeyboardButton("Drop", callback_data=f"pd:{token}"),
            ]
        ])
        await update.message.reply_text(
                f"{item['file']}\nModified: {item['modified_at']}",
            reply_markup=keyboard,
        )


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    subject = " ".join(context.args).strip()
    if not subject:
        await update.message.reply_text("Usage: /memory TICKER_OR_THEME_OR_AUTHOR")
        return
    from scripts.research_memory import subject_snapshot

    snapshot = await _run_blocking(subject_snapshot, subject, 8)
    await _send_long(update, snapshot)


async def cmd_mapstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    from scripts.research_memory import status

    stats = await _run_blocking(status)
    text = (
        "Structured research memory\n"
        f"Sources: {stats['research_sources']}\n"
        f"Views: {stats['research_views']}\n"
        f"Estimates/data points: {stats['research_estimates']}\n"
        f"Ratings: {stats['research_ratings']}\n"
        f"Changes: {stats['research_changes']}\n"
        f"Debates: {stats['research_debates']}\n"
        f"Research PDFs: {stats['research_pdfs']}\n"
        f"PDFs missing extraction JSON: {stats['pdfs_missing_extraction_json']}"
    )
    await update.message.reply_text(text)


async def cmd_teach(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    lesson = " ".join(context.args).strip()
    if not lesson:
        await update.message.reply_text("Usage: /teach LESSON")
        return
    from scripts.learning import add_lesson

    lesson_id = await _run_blocking(add_lesson, lesson, source="telegram")
    await update.message.reply_text(f"Saved analyst lesson #{lesson_id}.")


async def cmd_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    feedback = " ".join(context.args).strip()
    if not feedback:
        await update.message.reply_text("Usage: /feedback TEXT")
        return
    from scripts.learning import record_feedback

    user_id = update.effective_user.id if update.effective_user else None
    feedback_id = await _run_blocking(record_feedback, feedback, user_id=user_id)
    await update.message.reply_text(
        f"Saved feedback #{feedback_id}. I will apply it to future analyst answers."
    )


async def cmd_lessons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    from scripts.learning import format_lessons

    text = await _run_blocking(format_lessons, 30)
    await _send_long(update, text)


async def handle_pending_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not _is_allowed(update):
        await query.answer("Not authorised.", show_alert=True)
        return
    await query.answer()
    data = query.data or ""
    try:
        action, token = data.split(":", 1)
    except ValueError:
        await query.edit_message_text("Unknown pending action.")
        return
    from scripts.jobs import enqueue_job
    from scripts.ops import find_pending_by_token

    path = find_pending_by_token(token)
    if not path:
        await query.edit_message_text("Pending file not found.")
        return
    if action == "pc":
        job_id = enqueue_job("confirm_pending", {"path": str(path), "notify": True}, dedupe_key=f"confirm_pending:{token}")
        await query.edit_message_text(f"Queued confirm for {path.name}. Job #{job_id}.")
    elif action == "pd":
        job_id = enqueue_job("drop_pending", {"path": str(path), "notify": True}, dedupe_key=f"drop_pending:{token}")
        await query.edit_message_text(f"Queued drop for {path.name}. Job #{job_id}.")
    elif action == "pr":
        await query.edit_message_text(
            "Use /research TICKER, /macro AUTHOR, or /thematic THEME, then upload the file again."
        )
    else:
        await query.edit_message_text("Unknown pending action.")


async def handle_headline_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not _is_allowed(update):
        await query.answer("Not authorised.", show_alert=True)
        return
    data = query.data or ""
    try:
        _, key = data.split(":", 1)
    except ValueError:
        await query.message.reply_text("Unknown headline action.")
        return
    from scripts.jobs import enqueue_job
    from scripts.headlines import get_headline

    item = get_headline(key)
    if not item:
        await query.message.reply_text("Could not find that headline in the latest headline state.")
        return
    job_id = enqueue_job(
        "analyse_headline",
        {"key": key, "notify": True},
        dedupe_key=f"analyse_headline:{key}:{datetime.now().strftime('%Y%m%d%H%M%S')}",
    )
    try:
        await query.answer("Queued headline analysis.")
        await query.message.reply_text(
            f"Queued analysis for headline #{item.get('rank', '?')}: {item.get('title', '')[:160]}\n"
            f"Job #{job_id}."
        )
    except Exception as e:
        log.warning("headline ack to user failed (job %s still queued): %s", job_id, e)


# ---------------------------------------------------------------------------
# Document handler — the main flow
# ---------------------------------------------------------------------------

async def _queue_latest_headline_by_rank(update: Update, rank: int) -> bool:
    from scripts.jobs import enqueue_job
    from scripts.headlines import get_headline_by_rank

    item = get_headline_by_rank(rank)
    if not item:
        await update.message.reply_text("Could not find that headline in the latest Tech Brief.")
        return True
    key = item.get("key")
    if not key:
        await update.message.reply_text("That headline is missing its analysis key.")
        return True
    job_id = enqueue_job(
        "analyse_headline",
        {"key": key, "notify": True},
        dedupe_key=f"analyse_headline:{key}:{datetime.now().strftime('%Y%m%d%H%M%S')}",
    )
    try:
        await update.message.reply_text(
            f"Queued analysis for headline #{rank}: {item.get('title', '')[:160]}\n"
            f"Job #{job_id}."
        )
    except Exception as e:
        log.warning("headline ack to user failed (job %s still queued): %s", job_id, e)
    return True


async def _queue_latest_email_by_rank(update: Update, rank: int) -> bool:
    from scripts.jobs import enqueue_job
    from scripts.email_sweep import get_email_by_rank

    item = get_email_by_rank(rank)
    if not item:
        await update.message.reply_text("Could not find that email in the latest email sweep.")
        return True
    key = item.get("key")
    if not key:
        await update.message.reply_text("That email is missing its analysis key.")
        return True
    job_id = enqueue_job(
        "analyse_email",
        {"key": key, "notify": True},
        dedupe_key=f"analyse_email:{key}:{datetime.now().strftime('%Y%m%d%H%M%S')}",
    )
    await update.message.reply_text(
        f"Queued analysis for email #{rank}: {item.get('subject', '')[:160]}\n"
        f"Job #{job_id}."
    )
    return True


async def _queue_latest_scan_item_by_rank(update: Update, rank: int) -> bool:
    from scripts.jobs import enqueue_job
    from scripts.folder_scan import get_scan_item_by_rank

    item = get_scan_item_by_rank(rank)
    if not item:
        await update.message.reply_text(f"Could not find item #{rank} in the latest inbox scan.")
        return True
    stored_path = item.get("stored_path")
    if not stored_path:
        await update.message.reply_text("That item is missing its stored path.")
        return True
    job_id = enqueue_job(
        "analyse_inbox_file",
        {
            "stored_path": stored_path,
            "json_path": item.get("json_path"),
            "triage": item.get("triage") or {},
            "notify": True,
        },
        dedupe_key=f"analyse_inbox_file:{stored_path}:{datetime.now().strftime('%Y%m%d%H%M%S')}",
    )
    try:
        await update.message.reply_text(
            f"Queued analysis for inbox item #{rank}: {item.get('title', '')[:160]}\n"
            f"Job #{job_id}."
        )
    except Exception as e:
        log.warning("inbox item ack to user failed (job %s still queued): %s", job_id, e)
    return True


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)

    if update.message is None:
        return  # edited caption on an already-processed upload — nothing to do
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
            persistent_path = _persist_upload(tmp_path)
            from scripts.jobs import enqueue_job
            from scripts.kb import file_hash

            if override:
                job_id = enqueue_job(
                    "store_override",
                    {"path": str(persistent_path), **override, "notify": True},
                    dedupe_key=f"store_override:{file_hash(persistent_path)}:{override['mode']}:{override['arg']}",
                )
                await update.message.reply_text(
                    f"Queued forced ingest ({override['mode']} -> {override['arg']}). Job #{job_id}."
                )
            else:
                job_id = enqueue_job(
                    "ingest_file",
                    {"path": str(persistent_path), "notify": True},
                    dedupe_key=f"ingest_file:{file_hash(persistent_path)}",
                )
                await update.message.reply_text(
                    f"Queued {doc.file_name} for ingestion. Job #{job_id}.\n"
                    "The worker will process it in the background and send the analysis here when finished."
                )
        except Exception as e:
            log.exception("queue failed")
            await update.message.reply_text(f"Could not queue ingestion: {e}")


def _persist_upload(tmp_path: Path) -> Path:
    from scripts.kb import slugify

    TELEGRAM_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    safe_stem = slugify(tmp_path.stem, max_len=100)
    dest = TELEGRAM_UPLOAD_DIR / f"{stamp}_{safe_stem}{tmp_path.suffix.lower()}"
    shutil.copy2(tmp_path, dest)
    return dest


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
    text = (update.effective_message.text or "").strip() if update.effective_message else ""
    command = text.split()[0] if text else ""
    if command.startswith("/headline_"):
        rank_text = command.split("_", 1)[1].split("@", 1)[0]
        if rank_text.isdigit():
            await _queue_latest_headline_by_rank(update, int(rank_text))
            return
    if command.startswith("/email_"):
        rank_text = command.split("_", 1)[1].split("@", 1)[0]
        if rank_text.isdigit():
            await _queue_latest_email_by_rank(update, int(rank_text))
            return
    if command.startswith("/inbox_"):
        rank_text = command.split("_", 1)[1].split("@", 1)[0]
        if rank_text.isdigit():
            await _queue_latest_scan_item_by_rank(update, int(rank_text))
            return
    await update.message.reply_text("Unknown command. /help for the list.")


async def _handle_ingest_snippet(update: Update, raw: str) -> None:
    """Save a pasted text block as a .txt file and queue it for auto-classification."""
    import re
    from scripts.kb import file_hash, slugify
    from scripts.jobs import enqueue_job

    # First line is the trigger ("ingest this note by SigmaIntell"); body is the rest.
    parts = raw.split("\n", 1)
    trigger_line = parts[0].strip()
    body = parts[1].strip() if len(parts) > 1 else ""

    if len(body) < 20:
        await update.message.reply_text(
            "No content found after the trigger line.\n\n"
            "Format:\n"
            "  ingest this note by AUTHOR\n\n"
            "  {paste text here}\n\n"
            "Or without an author:\n"
            "  ingest this\n\n"
            "  {paste text here}"
        )
        return

    # Extract optional author hint: "by {Author}" at end of trigger line.
    author_hint = None
    m = re.search(r"\bby\s+(.+)$", trigger_line, re.IGNORECASE)
    if m:
        author_hint = m.group(1).strip().strip("\"'")

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    if author_hint:
        safe_author = slugify(author_hint, max_len=60)
        fname = f"{safe_author}_{stamp}.txt"
    else:
        fname = f"snippet_{stamp}.txt"

    TELEGRAM_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = TELEGRAM_UPLOAD_DIR / fname
    dest.write_text(body, encoding="utf-8")

    fhash = file_hash(dest)
    job_id = enqueue_job(
        "ingest_file",
        {"path": str(dest), "notify": True},
        dedupe_key=f"ingest_file:{fhash}",
    )
    log.info("snippet ingestion queued: file=%s job=%s", fname, job_id)
    try:
        await update.message.reply_text(
            f"Saved as {fname} ({len(body):,} chars).\n"
            f"Queued for classification + storage. Job #{job_id}.\n"
            f"The worker will triage, store, and send the summary here."
        )
    except Exception as e:
        log.warning("snippet ack to user failed (job %s still queued): %s", job_id, e)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return await _deny(update)
    # Edited messages (typo fixes) arrive with update.message=None; treat the
    # corrected text as a fresh question instead of crashing on .text.
    msg = update.effective_message
    if msg is None:
        return
    text = (msg.text or "").strip()
    lower = text.lower()
    if _is_tech_brief_status_query(text):
        await _send_long_html(update, _tech_brief_status_text())
        return
    if lower.startswith("ingest this"):
        await _handle_ingest_snippet(update, text)
        return
    await _queue_analyst_question(update, text, source="plain-text")


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
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("note", cmd_note))
    app.add_handler(CommandHandler("download", cmd_download))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("mapstatus", cmd_mapstatus))
    app.add_handler(CommandHandler("teach", cmd_teach))
    app.add_handler(CommandHandler("feedback", cmd_feedback))
    app.add_handler(CommandHandler("lessons", cmd_lessons))
    app.add_handler(CallbackQueryHandler(handle_pending_action, pattern=r"^p[crd]:"))
    app.add_handler(CallbackQueryHandler(handle_headline_action, pattern=r"^ha:"))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.COMMAND, handle_unknown))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)

    log.info("bot starting; allowed user IDs: %s", sorted(ALLOWED))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
