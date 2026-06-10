"""Provider-neutral LLM helpers for JSON extraction tasks."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any


class LLMProviderError(RuntimeError):
    pass


_CLIENT_CACHE: dict[tuple, Any] = {}


def get_client(provider: str, *, timeout: float = 180.0, max_retries: int = 0):
    """Return a cached SDK client (thread-safe, holds a connection pool)."""
    key = (provider, timeout, max_retries)
    client = _CLIENT_CACHE.get(key)
    if client is None:
        if provider == "openai":
            from openai import OpenAI

            client = OpenAI(timeout=timeout, max_retries=max_retries)
        else:
            from anthropic import Anthropic

            client = Anthropic(timeout=timeout, max_retries=max_retries)
        _CLIENT_CACHE[key] = client
    return client


def cached_system_block(system: str) -> list[dict]:
    """Anthropic system param with a prompt-cache marker on the static block."""
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


def parse_json_loose(text: str):
    """Parse JSON from a model response, stripping markdown code fences.

    Unlike parse_json_response this raises json.JSONDecodeError (which
    research/macro/thematic store flows catch for their retry loop) and
    allows non-object JSON.
    """
    text = (text or "").strip()
    md_match = re.match(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if md_match:
        text = md_match.group(1).strip()
    return json.loads(text)


def call_api(client, messages, max_tokens=8192, system=None, return_response=False,
             model=None, default_model="claude-opus-4-7"):
    """Anthropic call with transient-error retry + streaming fallback.

    Shared by research/macro/thematic (was triplicated there).

    system: list of content blocks (use cached_system_block for prompt
            caching) or a plain string.
    return_response: return the raw response object instead of just the text
                     (so callers can inspect usage metadata).
    """
    chosen_model = model or default_model
    for attempt in range(3):
        try:
            kwargs = {
                "model": chosen_model,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            if system is not None:
                kwargs["system"] = system
            response = client.messages.create(**kwargs)
            if return_response:
                return response
            return response.content[0].text
        except Exception as e:
            err_str = str(e).lower()
            if "overloaded" in err_str or "connection" in err_str or "529" in err_str or "disconnected" in err_str:
                if attempt < 2:
                    wait = 5 * (attempt + 1)
                    print(f"  API transient error, retrying in {wait}s (streaming)...")
                    time.sleep(wait)
                    # Fall back to streaming to keep the connection alive
                    try:
                        stream_kwargs = {
                            "model": chosen_model,
                            "max_tokens": max_tokens,
                            "messages": messages,
                        }
                        if system is not None:
                            stream_kwargs["system"] = system
                        chunks = []
                        with client.messages.stream(**stream_kwargs) as stream:
                            for text in stream.text_stream:
                                chunks.append(text)
                            final = stream.get_final_message()
                        # Truncated streams (max_tokens hit, connection drop) must not
                        # return partial chunks — fall through to the next retry.
                        if final.stop_reason not in ("end_turn", "stop_sequence"):
                            print(f"  stream stop_reason={final.stop_reason!r}, retrying...")
                            continue
                        if return_response:
                            return final
                        return "".join(chunks)
                    except Exception as inner:
                        print(f"  streaming retry failed: {inner}, trying again...")
                        continue
            raise
    raise RuntimeError("API call failed after 3 attempts")


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    temperature: float = 0.0
    max_retries: int = 3
    timeout: float = 180.0


def _clean_json_text(text: str) -> str:
    text = (text or "").strip()
    match = re.match(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last != -1 and last > first:
        text = text[first:last + 1]
    return text


def parse_json_response(text: str) -> dict[str, Any]:
    cleaned = _clean_json_text(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise LLMProviderError(f"LLM response was not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise LLMProviderError("LLM response JSON was not an object")
    return data


def _openai_text(response) -> str:
    text = getattr(response, "output_text", None)
    if text:
        return text
    chunks = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            value = getattr(content, "text", None)
            if value:
                chunks.append(value)
    return "\n".join(chunks)


def _reasoning_effort(model: str) -> str:
    override = os.environ.get("OPENAI_REASONING_EFFORT")
    if override:
        return override
    model_l = model.lower()
    if model_l.startswith(("gpt-5.1", "gpt-5.2")):
        return "low"
    return "minimal"


def complete_json(prompt: str, *, config: LLMConfig, system: str | None = None,
                  max_output_tokens: int = 8192) -> dict[str, Any]:
    """Call the configured provider and parse a JSON object response."""
    provider = config.provider.lower().strip()
    last_error: Exception | None = None
    for attempt in range(1, config.max_retries + 1):
        try:
            if provider == "openai":
                if not os.environ.get("OPENAI_API_KEY"):
                    raise LLMProviderError("OPENAI_API_KEY is required for OpenAI extraction")
                client = get_client("openai", timeout=config.timeout)
                messages = []
                if system:
                    messages.append({"role": "system", "content": system})
                messages.append({"role": "user", "content": prompt})
                request_tokens = min(32768, max(1000, max_output_tokens * attempt))
                kwargs = {
                    "model": config.model,
                    "input": messages,
                    "text": {"format": {"type": "json_object"}},
                    "max_output_tokens": request_tokens,
                }
                if config.model.lower().startswith("gpt-5"):
                    kwargs["reasoning"] = {"effort": _reasoning_effort(config.model)}
                if request_tokens >= 4096 or os.environ.get("OPENAI_RESPONSES_STREAM", "1") == "1":
                    with client.responses.stream(**kwargs) as stream:
                        response = stream.get_final_response()
                else:
                    response = client.responses.create(**kwargs)
                text = _openai_text(response)
                if not text:
                    status = getattr(response, "status", None)
                    details = getattr(response, "incomplete_details", None)
                    raise LLMProviderError(
                        f"OpenAI returned no output text; status={status}, details={details}"
                    )
                return parse_json_response(text)

            if provider in {"anthropic", "claude"}:
                if not os.environ.get("ANTHROPIC_API_KEY"):
                    raise LLMProviderError("ANTHROPIC_API_KEY is required for Anthropic extraction")
                client = get_client("anthropic", timeout=config.timeout)
                kwargs = {
                    "model": config.model,
                    "max_tokens": max_output_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                }
                if system:
                    kwargs["system"] = cached_system_block(system)
                response = client.messages.create(**kwargs)
                return parse_json_response(response.content[0].text)

            raise LLMProviderError(f"Unsupported LLM provider: {config.provider}")
        except Exception as e:
            last_error = e
            if attempt >= config.max_retries:
                break
            time.sleep(min(30, 3 * attempt))
    raise LLMProviderError(str(last_error)) from last_error


def env_config(kind: str, default_provider: str, default_model: str,
               timeout: float = 180.0) -> LLMConfig:
    prefix = kind.upper()
    return LLMConfig(
        provider=os.environ.get(f"{prefix}_PROVIDER", default_provider),
        model=os.environ.get(f"{prefix}_MODEL", default_model),
        timeout=float(os.environ.get(f"{prefix}_TIMEOUT", timeout)),
        max_retries=int(os.environ.get(f"{prefix}_MAX_RETRIES", "3")),
    )


def missing_credentials(config: LLMConfig) -> str | None:
    provider = config.provider.lower().strip()
    if provider == "openai" and not os.environ.get("OPENAI_API_KEY"):
        return "OPENAI_API_KEY"
    if provider in {"anthropic", "claude"} and not os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY"
    return None
