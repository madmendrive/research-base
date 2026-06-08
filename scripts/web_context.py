"""Optional live web context for analyst answers."""

from __future__ import annotations

import os
import re


TEMPORAL_TRIGGERS = {
    "latest",
    "today",
    "current",
    "currently",
    "now",
    "recent",
    "news",
    "headline",
    "headlines",
    "price",
    "market cap",
    "estimates",
    "estimate",
    "guidance",
    "earnings",
    "filing",
    "filings",
    "announced",
    "reported",
    "updated",
}

SOURCE_CONSTRAINED_HINTS = {
    "according to jpm",
    "jpm's",
    "jp morgan",
    "j.p. morgan",
    "semianalysis",
    "semi analysis",
    "according to semi",
    "my notes",
    "your notes",
    "kb",
    "knowledge base",
}

EXPLICIT_WEB_HINTS = {
    "web",
    "search",
    "google",
    "live",
    "today",
    "current share price",
    "share price today",
    "market cap",
    "news",
    "headline",
    "filing",
    "filings",
}


def should_use_web(query: str, *, kb_result_count: int = 0, has_structured_context: bool = False) -> bool:
    mode = os.environ.get("ANALYST_WEB_CONTEXT", "auto").strip().lower()
    if mode in {"0", "false", "no", "off", "disabled"}:
        return False
    if mode in {"1", "true", "yes", "on", "always"}:
        return True

    query_l = (query or "").lower()
    if has_structured_context and any(hint in query_l for hint in SOURCE_CONSTRAINED_HINTS):
        if not any(hint in query_l for hint in EXPLICIT_WEB_HINTS):
            return False
    if any(trigger in query_l for trigger in TEMPORAL_TRIGGERS):
        return True
    if re.search(r"\b20[2-9][0-9]\b", query_l) and any(
        word in query_l for word in ("view", "estimate", "forecast", "guidance", "growth")
    ):
        return True
    if kb_result_count == 0 and not has_structured_context:
        return os.environ.get("ANALYST_WEB_ON_EMPTY_KB", "1").strip().lower() not in {"0", "false", "no"}
    return False


def _prompt(query: str, max_items: int) -> str:
    return f"""Find current web context for this investment-research question:
{query}

Return at most {max_items} bullets for a buy-side analyst. Each bullet must include
the source name and URL. Prefer primary sources, company IR, regulatory filings,
exchange releases, reputable financial news, and industry sources. Include dates
and numbers where visible. Do not write the final investment answer; only provide
current external context for another analyst model to use."""


def _call_openai_web(query: str, *, max_items: int, max_searches: int) -> str:
    if not os.environ.get("OPENAI_API_KEY"):
        return ""
    from openai import OpenAI
    from scripts.llm_provider import _openai_text

    model = os.environ.get("ANALYST_WEB_MODEL", "gpt-5-mini")
    client = OpenAI(
        timeout=float(os.environ.get("ANALYST_WEB_TIMEOUT", "120")),
        max_retries=int(os.environ.get("ANALYST_WEB_MAX_RETRIES", "1")),
    )
    kwargs = {
        "model": model,
        "input": _prompt(query, max_items),
        "tools": [
            {
                "type": "web_search",
                "external_web_access": os.environ.get("ANALYST_WEB_LIVE", "1").strip().lower()
                not in {"0", "false", "no", "off"},
            }
        ],
        "tool_choice": "auto",
        "max_output_tokens": int(os.environ.get("ANALYST_WEB_MAX_OUTPUT_TOKENS", "1800")),
    }
    if model.lower().startswith("gpt-5"):
        kwargs["reasoning"] = {"effort": os.environ.get("ANALYST_WEB_REASONING_EFFORT", "low")}
    response = client.responses.create(**kwargs)
    return _openai_text(response).strip()


def _call_anthropic_web(query: str, *, max_items: int, max_searches: int) -> str:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return ""
    from anthropic import Anthropic

    model = os.environ.get("ANALYST_WEB_MODEL") or os.environ.get("ANALYST_MODEL", "claude-opus-4-7")
    client = Anthropic(
        timeout=float(os.environ.get("ANALYST_WEB_TIMEOUT", "120")),
        max_retries=int(os.environ.get("ANALYST_WEB_MAX_RETRIES", "1")),
    )
    resp = client.messages.create(
        model=model,
        max_tokens=int(os.environ.get("ANALYST_WEB_MAX_OUTPUT_TOKENS", "1800")),
        tools=[
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": max_searches,
                "user_location": {"type": "approximate", "timezone": "Asia/Hong_Kong"},
            }
        ],
        messages=[{"role": "user", "content": _prompt(query, max_items)}],
    )
    return "\n".join(
        getattr(block, "text", "")
        for block in resp.content
        if getattr(block, "type", None) == "text"
    ).strip()


def fetch_web_context(query: str, *, kb_result_count: int = 0, has_structured_context: bool = False) -> str:
    if not should_use_web(query, kb_result_count=kb_result_count, has_structured_context=has_structured_context):
        return ""

    provider = os.environ.get("ANALYST_WEB_PROVIDER", "openai").strip().lower()
    max_items = int(os.environ.get("ANALYST_WEB_MAX_ITEMS", "6"))
    max_searches = int(os.environ.get("ANALYST_WEB_MAX_USES", "4"))
    try:
        if provider in {"anthropic", "claude"}:
            text = _call_anthropic_web(query, max_items=max_items, max_searches=max_searches)
        else:
            text = _call_openai_web(query, max_items=max_items, max_searches=max_searches)
    except Exception as e:
        if os.environ.get("ANALYST_WEB_FAIL_CLOSED", "0").strip().lower() in {"1", "true", "yes"}:
            raise
        text = f"Live web context was requested but failed: {type(e).__name__}: {e}"

    return f"<live_web_context>\n{text}\n</live_web_context>" if text else ""
