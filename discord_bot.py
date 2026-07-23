"""Discord bot: analyse commands + analyst Q&A, mirroring the Telegram bot.

Commands (from an allowed user, any channel the bot can read):
  /headline_N   analyse item N from the latest Tech Brief
  /email_N      analyse item N from the latest email sweep
  /inbox_N      analyse item N from the latest inbox scan

Analyst questions: mention the bot (@BotName your question) or DM it.
Digests themselves arrive via channel webhooks (scripts/notify.discord_send);
this bot only handles the interactive direction. Replies for jobs queued here
are routed back to the originating Discord channel via reply_via.

Env: DISCORD_BOT_TOKEN (required), DISCORD_ALLOWED_USER_IDS (comma-separated;
required — the bot ignores everyone else, same single-user model as bot.py).
"""

import truststore

truststore.inject_into_ssl()

import logging
import os
import re
from datetime import datetime

import discord
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("discord_bot")

ALLOWED_USER_IDS = {
    int(x) for x in os.environ.get("DISCORD_ALLOWED_USER_IDS", "").split(",") if x.strip()
}

CMD_RE = re.compile(r"^/(headline|email|inbox)_(\d+)\s*$")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


def _reply_via(message: discord.Message) -> dict:
    return {"channel": "discord", "channel_id": message.channel.id}


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S")


def _queue_headline(rank: int, reply_via: dict) -> str:
    from scripts.headlines import get_headline_by_rank
    from scripts.jobs import enqueue_job

    item = get_headline_by_rank(rank)
    if not item:
        return "Could not find that headline in the latest Tech Brief."
    key = item.get("key")
    if not key:
        return "That headline is missing its analysis key."
    job_id = enqueue_job(
        "analyse_headline",
        {"key": key, "notify": True, "reply_via": reply_via},
        dedupe_key=f"analyse_headline:{key}:{_stamp()}",
    )
    return f"Queued analysis for headline #{rank}: {item.get('title', '')[:160]}\nJob #{job_id}."


def _queue_email(rank: int, reply_via: dict) -> str:
    from scripts.email_sweep import get_email_by_rank
    from scripts.jobs import enqueue_job

    item = get_email_by_rank(rank)
    if not item:
        return "Could not find that email in the latest email sweep."
    key = item.get("key")
    if not key:
        return "That email is missing its analysis key."
    job_id = enqueue_job(
        "analyse_email",
        {"key": key, "notify": True, "reply_via": reply_via},
        dedupe_key=f"analyse_email:{key}:{_stamp()}",
    )
    return f"Queued analysis for email #{rank}: {item.get('subject', '')[:160]}\nJob #{job_id}."


def _queue_inbox(rank: int, reply_via: dict) -> str:
    from scripts.folder_scan import get_scan_item_by_rank
    from scripts.jobs import enqueue_job

    item = get_scan_item_by_rank(rank)
    if not item:
        return f"Could not find item #{rank} in the latest inbox scan."
    stored_path = item.get("stored_path")
    if not stored_path:
        return "That item is missing its stored path."
    job_id = enqueue_job(
        "analyse_inbox_file",
        {
            "stored_path": stored_path,
            "json_path": item.get("json_path"),
            "triage": item.get("triage") or {},
            "notify": True,
            "reply_via": reply_via,
        },
        dedupe_key=f"analyse_inbox_file:{stored_path}:{_stamp()}",
    )
    return f"Queued analysis for inbox item #{rank}: {item.get('title', '')[:160]}\nJob #{job_id}."


def _queue_question(question: str, user_id: int, reply_via: dict) -> str:
    from scripts.jobs import enqueue_job

    job_id = enqueue_job(
        "analyst_question",
        {"question": question, "user_id": user_id, "reply_via": reply_via},
        dedupe_key=f"analyst_question:discord:{user_id}:{_stamp()}",
    )
    return f"Working on it (job #{job_id}) — the answer will land in this channel."


QUEUERS = {"headline": _queue_headline, "email": _queue_email, "inbox": _queue_inbox}


@client.event
async def on_ready() -> None:
    log.info("discord bot logged in as %s (id=%s); allowed users: %s",
             client.user, client.user.id, sorted(ALLOWED_USER_IDS))


@client.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return
    if not ALLOWED_USER_IDS or message.author.id not in ALLOWED_USER_IDS:
        return
    text = (message.content or "").strip()
    if not text:
        return

    m = CMD_RE.match(text.split("@", 1)[0].strip())
    if m:
        kind, rank = m.group(1), int(m.group(2))
        try:
            ack = QUEUERS[kind](rank, _reply_via(message))
        except Exception as e:
            log.exception("failed to queue %s_%d", kind, rank)
            ack = f"Failed to queue: {type(e).__name__}: {e}"
        await message.reply(ack, mention_author=False)
        return

    # Analyst questions: DM, or a message that mentions the bot.
    is_dm = message.guild is None
    mentioned = client.user in message.mentions if client.user else False
    if is_dm or mentioned:
        question = re.sub(rf"<@!?{client.user.id}>", "", text).strip() if client.user else text
        if not question:
            await message.reply(
                "Ask a question after the mention, or use /headline_N, /email_N, /inbox_N.",
                mention_author=False,
            )
            return
        ack = _queue_question(question, message.author.id, _reply_via(message))
        await message.reply(ack, mention_author=False)


def main() -> None:
    token = (os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set in .env")
    if not ALLOWED_USER_IDS:
        raise SystemExit("DISCORD_ALLOWED_USER_IDS is not set in .env")
    client.run(token, log_handler=None)


if __name__ == "__main__":
    main()
