"""Podcast / video transcript handling.

Transcripts are plain-text files (named `transcript-*.txt`) with a header:

    Title: ...
    URL: https://www.youtube.com/watch?v=...
    Video ID: ...
    Channel: <podcast / show name>
    Transcript source: youtube

    Transcript:
    <spoken text>

They are author-driven commentary, so they route through the existing macro/
semis author pipeline with the *show* as the recurring author (primary_subject)
and the human speakers captured in the triage `author` field.
"""
import re


def parse_transcript(text: str) -> dict | None:
    """Return {title, channel, url, video_id} from a transcript header, or None
    if `text` doesn't look like one of our transcript files."""
    if not text:
        return None
    head = text[:1500]
    if "Transcript source:" not in head and "Channel:" not in head:
        return None

    def field(name: str) -> str:
        m = re.search(rf"^{name}:[ \t]*(.+)$", head, re.MULTILINE)
        return m.group(1).strip() if m else ""

    channel = field("Channel")
    return {
        "title": field("Title"),
        "channel": channel if channel and channel.lower() != "unknown" else "",
        "url": field("URL"),
        "video_id": field("Video ID"),
    }


def is_transcript_path(path) -> bool:
    return str(path).lower().endswith(".txt")


def transcript_routing_hint(meta: dict) -> str:
    """A preamble prepended to the transcript text sent to triage so the show is
    routed as the author and the speakers are captured."""
    channel = (meta.get("channel") or "").strip()
    title = meta.get("title") or ""
    if channel:
        author_block = (
            f"Podcast / show (Channel): {channel}\n"
            f"Episode title: {title}\n"
            "Routing instructions (override the generic rules with these):\n"
            "- Set primary_type = \"macro\".\n"
            "- The recurring AUTHOR is the show itself. Set primary_subject and "
            f"\"publisher\" to the show name: \"{channel}\". If \"{channel}\" is not "
            "already in the author lists, propose it (proposed_new_semis_authors if "
            "it is a semiconductor / tech-equity show such as SemiAnalysis, else "
            "proposed_new_macro_authors), and set \"category\" to \"Semis\" or "
            f"\"Macro\" to match. Use EXACTLY \"{channel}\" as the proposed author "
            "name (no parenthetical outlet) so it matches primary_subject.\n"
        )
    else:
        author_block = (
            f"Episode title: {title}\n"
            "The channel / show name is UNKNOWN. Routing instructions:\n"
            "- Set primary_type = \"macro\", category = \"Macro\".\n"
            "- Set primary_subject and \"publisher\" to \"Others\" (the catch-all "
            "bucket for transcripts whose show is unknown) and propose \"Others\" as "
            "a new macro author with that exact name. Do not invent a show name.\n"
        )
    return (
        "=== INGESTION NOTE — THIS IS A PODCAST / VIDEO TRANSCRIPT ===\n"
        + author_block
        + "- Put the individual human SPEAKERS (host plus any guests, by name) in "
        "the \"author\" field.\n"
        "- Still identify every ticker and theme substantively discussed, with "
        "materiality.\n"
        "=== TRANSCRIPT BODY FOLLOWS ===\n\n"
    )
