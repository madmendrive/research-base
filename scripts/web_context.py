"""Optional live web context for analyst answers."""

from __future__ import annotations

import logging
import os
import re
import time

log = logging.getLogger(__name__)


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

# A direct request to consult the web. Word-boundary matched so "web" doesn't
# fire on "website" and "search" doesn't fire on "research". "live" is paired
# with web/search/online only, since bare "live" is too ambiguous.
_EXPLICIT_WEB_REQUEST_RE = re.compile(
    r"\b(?:web|google|internet|online|web\s*search|search\s+(?:the\s+)?web|"
    r"live\s+(?:web|search|internet))\b",
    re.IGNORECASE,
)


def should_use_web(query: str, *, kb_result_count: int = 0, has_structured_context: bool = False) -> bool:
    mode = os.environ.get("ANALYST_WEB_CONTEXT", "auto").strip().lower()
    if mode in {"0", "false", "no", "off", "disabled"}:
        return False
    if mode in {"1", "true", "yes", "on", "always"}:
        return True

    query_l = (query or "").lower()
    # An explicit web request always wins, even when the KB already has hits.
    # Previously "web"/"search" only suppressed source-constrained gating; they
    # were never a positive trigger, so "check the live web" with KB context
    # fell through to False.
    if _EXPLICIT_WEB_REQUEST_RE.search(query_l):
        return True
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

    # ANALYST_WEB_MODEL may be an OpenAI model (it's the OpenAI web model in
    # this deployment); never send that to the Anthropic API.
    model = os.environ.get("ANALYST_WEB_ANTHROPIC_MODEL")
    if not model:
        candidate = os.environ.get("ANALYST_WEB_MODEL", "")
        model = candidate if candidate and not candidate.lower().startswith(("gpt-", "o")) else ""
    model = model or os.environ.get("ANALYST_MODEL") or "claude-opus-4-8"
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


def _is_transient_web_error(exc: Exception) -> bool:
    s = f"{type(exc).__name__} {exc}".lower()
    return any(t in s for t in (
        "rate", "429", "overloaded", "529", "503", "502",
        "timeout", "timed out", "connection", "temporarily",
    ))


def _run_web_provider(provider: str, query: str, *, max_items: int, max_searches: int) -> str:
    if provider in {"anthropic", "claude"}:
        return _call_anthropic_web(query, max_items=max_items, max_searches=max_searches)
    return _call_openai_web(query, max_items=max_items, max_searches=max_searches)


def fetch_web_context(query: str, *, kb_result_count: int = 0, has_structured_context: bool = False) -> str:
    if not should_use_web(query, kb_result_count=kb_result_count, has_structured_context=has_structured_context):
        return ""

    max_items = int(os.environ.get("ANALYST_WEB_MAX_ITEMS", "6"))
    max_searches = int(os.environ.get("ANALYST_WEB_MAX_USES", "4"))
    attempts = max(1, int(os.environ.get("ANALYST_WEB_ATTEMPTS", "2")))

    primary = os.environ.get("ANALYST_WEB_PROVIDER", "openai").strip().lower()
    primary = "anthropic" if primary in {"anthropic", "claude"} else "openai"
    order = [primary]
    if os.environ.get("ANALYST_WEB_FALLBACK", "1").strip().lower() not in {"0", "false", "no"}:
        # OpenAI and Anthropic web search are independent APIs, so an upstream
        # rate limit on one rarely coincides with the other.
        order.append("anthropic" if primary == "openai" else "openai")

    errors: list[str] = []
    for provider in order:
        for attempt in range(attempts):
            try:
                text = _run_web_provider(provider, query, max_items=max_items, max_searches=max_searches)
            except Exception as e:  # noqa: BLE001 — provider SDKs raise varied types
                errors.append(f"{provider}: {type(e).__name__}: {e}")
                log.warning("web context %s attempt %d/%d failed: %s",
                            provider, attempt + 1, attempts, e)
                if _is_transient_web_error(e) and attempt + 1 < attempts:
                    time.sleep(2 * (attempt + 1))
                    continue
                break  # non-transient, or attempts exhausted → next provider
            if text:
                return f"<live_web_context>\n{text}\n</live_web_context>"
            break  # empty (e.g. provider has no API key) → next provider

    if os.environ.get("ANALYST_WEB_FAIL_CLOSED", "0").strip().lower() in {"1", "true", "yes"}:
        raise RuntimeError("; ".join(errors) or "web context unavailable")
    detail = "; ".join(errors) if errors else "no web provider available"
    return f"<live_web_context>\nLive web context was requested but failed: {detail}\n</live_web_context>"
