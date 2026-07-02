"""Overnight study agent for durable company/theme dossiers.

This module distills the existing KB and structured research memory into
study notes. It does not train model weights; it writes better memory artifacts
that future analyst answers can retrieve.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts import kb
from scripts.analyst import _call_anthropic, _call_openai


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
STUDY_DIR = DATA_DIR / "_study"
DOSSIER_DIR = STUDY_DIR / "dossiers"
RUN_DIR = STUDY_DIR / "runs"
STATE_PATH = STUDY_DIR / "study_state.json"

DEFAULT_TOPICS = (
    "semiconductor",
    "semis",
    "memory",
    "dram",
    "nand",
    "hbm",
    "spe",
    "wfe",
    "ai infrastructure",
    "electronic components",
)

EXCLUDE_SUBJECTS = {
    "",
    "news article",
    "unknown",
    "n/a",
    "na",
    "ai infrastructure",
}


@dataclass(frozen=True)
class StudyConfig:
    scope: str = "all"
    topics: tuple[str, ...] = DEFAULT_TOPICS
    targets: tuple[str, ...] = ()
    since_hours: float = 0.0
    max_cost: float = 30.0
    limit: int = 0
    provider: str = "anthropic"
    model: str = "claude-opus-4-8"
    max_output_tokens: int = 8000  # headroom for adaptive-thinking tokens + dossier
    timeout: float = 600.0  # xhigh adaptive thinking can run minutes per dossier
    force: bool = False
    dry_run: bool = False
    embed: bool = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"studied": {}, "runs": []}
    return json.loads(STATE_PATH.read_text(encoding="utf-8", errors="replace"))


def _save_state(state: dict[str, Any]) -> None:
    STUDY_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text or "") / 4))


def _price_defaults(provider: str, model: str) -> tuple[float, float]:
    """Return conservative USD per 1M input/output tokens."""
    provider_l = (provider or "").lower()
    model_l = (model or "").lower()
    if provider_l in {"anthropic", "claude"}:
        # Opus 4.x is $5/$25 per MTok. The old $15/$75 default (Opus 4.1-era)
        # overstated cost 3x, so the study budget gate stopped runs at a third
        # of their actual allowance.
        if "opus" in model_l:
            return 5.0, 25.0
        if "sonnet" in model_l:
            return 3.0, 15.0
        return 5.0, 25.0
    if "mini" in model_l:
        return 1.0, 4.0
    if model_l.startswith("gpt-5"):
        return 5.0, 20.0
    return 5.0, 20.0


def _cost_estimate_usd(input_tokens: int, output_tokens: int, *, provider: str, model: str) -> float:
    default_in, default_out = _price_defaults(provider, model)
    in_cost = float(os.environ.get("STUDY_INPUT_COST_PER_MTOK", default_in))
    out_cost = float(os.environ.get("STUDY_OUTPUT_COST_PER_MTOK", default_out))
    return (input_tokens / 1_000_000) * in_cost + (output_tokens / 1_000_000) * out_cost


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    slug = re.sub(r"_+", "_", slug).strip("._-")
    return slug[:90] or "untitled"


def _scope_where(scope: str) -> tuple[str, list[str]]:
    scope_l = (scope or "all").lower()
    if scope_l == "jpm":
        return (
            """
            AND (
                lower(publisher) LIKE '%j.p. morgan%'
                OR lower(publisher) LIKE '%jpmorgan%'
                OR lower(publisher) LIKE '%jpm%'
                OR lower(source_uri) LIKE '%jpm_%'
            )
            """,
            [],
        )
    return "", []


def _topic_score(text: str, topics: tuple[str, ...]) -> int:
    text_l = (text or "").lower()
    score = 0
    for topic in topics:
        topic_l = topic.lower().replace("-", " ")
        if topic_l and topic_l in text_l.replace("-", " "):
            score += 3 if topic_l in {"memory", "dram", "nand", "hbm", "spe", "wfe"} else 1
    return score


def _normalise_target_filter(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _target_matches_filter(key: str, subject: str, subject_type: str, filters: set[str]) -> bool:
    if not filters:
        return True
    key_n = _normalise_target_filter(key)
    subject_n = _normalise_target_filter(subject)
    typed_subject_n = _normalise_target_filter(f"{subject_type}:{subject}")
    return key_n in filters or subject_n in filters or typed_subject_n in filters


def _path_mtime(value: str | None) -> float | None:
    if not value:
        return None
    try:
        path = Path(value)
        if not path.exists():
            return None
        return path.stat().st_mtime
    except OSError:
        return None


def _source_file_mtime(row: dict[str, Any]) -> float | None:
    mtimes = [
        mtime
        for mtime in (
            _path_mtime(row.get("json_path")),
            _path_mtime(row.get("source_path")),
        )
        if mtime is not None
    ]
    return max(mtimes) if mtimes else None


def _recent_source_mtimes(hours: float) -> dict[str, float]:
    if not hours or hours <= 0:
        return {}
    cutoff = time.time() - (hours * 3600)
    conn = kb.connect()
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT source_uri, json_path, source_path FROM research_sources"
            )
        ]
    finally:
        conn.close()

    recent: dict[str, float] = {}
    for row in rows:
        mtime = _source_file_mtime(row)
        if mtime is not None and mtime >= cutoff:
            recent[str(row["source_uri"])] = mtime
    return recent


def _select_targets(config: StudyConfig, state: dict[str, Any]) -> list[dict[str, Any]]:
    where_scope, params = _scope_where(config.scope)
    target_filters = {_normalise_target_filter(t) for t in config.targets if str(t).strip()}
    recent_sources = _recent_source_mtimes(config.since_hours)
    where_recent = ""
    recent_params: list[str] = []
    if config.since_hours and config.since_hours > 0:
        if not recent_sources:
            return []
        recent_params = list(recent_sources.keys())
        where_recent = f"AND source_uri IN ({','.join('?' for _ in recent_params)})"
    conn = kb.connect()
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                f"""
                WITH facts AS (
                    SELECT source_uri, subject_type, subject, publisher, author,
                           published_at, NULL AS title, theme AS context
                    FROM research_views
                    WHERE subject IS NOT NULL
                    UNION ALL
                    SELECT source_uri, subject_type, subject, publisher, author,
                           published_at, NULL AS title, metric || ' ' || coalesce(source_detail, '') AS context
                    FROM research_estimates
                    WHERE subject IS NOT NULL
                    UNION ALL
                    SELECT source_uri, 'ticker' AS subject_type, subject, publisher, author,
                           published_at, NULL AS title, rating AS context
                    FROM research_ratings
                    WHERE subject IS NOT NULL
                    UNION ALL
                    SELECT source_uri, subject_type, subject, publisher, author,
                           published_at, NULL AS title, debate AS context
                    FROM research_debates
                    WHERE subject IS NOT NULL
                )
                SELECT subject_type, subject,
                       COUNT(DISTINCT source_uri) AS source_count,
                       MAX(published_at) AS latest_date,
                       GROUP_CONCAT(DISTINCT publisher) AS publishers,
                       GROUP_CONCAT(DISTINCT context) AS contexts
                FROM facts
                WHERE lower(subject) NOT IN ({",".join("?" for _ in EXCLUDE_SUBJECTS)})
                  AND subject_type IN ('ticker', 'theme', 'single_name')
                  {where_scope}
                  {where_recent}
                GROUP BY subject_type, subject
                HAVING source_count >= 1
                ORDER BY source_count DESC, latest_date DESC
                """,
                [*EXCLUDE_SUBJECTS, *params, *recent_params],
            )
        ]
    finally:
        conn.close()

    studied = state.get("studied", {})
    targets: list[dict[str, Any]] = []
    for row in rows:
        subject = str(row.get("subject") or "").strip()
        subject_type = str(row.get("subject_type") or "").strip()
        if not subject or subject.lower() in EXCLUDE_SUBJECTS:
            continue
        key = f"{subject_type}:{subject}"
        if not _target_matches_filter(key, subject, subject_type, target_filters):
            continue
        if not config.force and not config.since_hours and not target_filters and studied.get(key):
            continue
        haystack = " ".join(
            str(row.get(k) or "")
            for k in ("subject", "subject_type", "publishers", "contexts")
        )
        score = _topic_score(haystack, config.topics)
        if config.topics and score <= 0 and not target_filters:
            continue
        targets.append({
            "key": key,
            "subject_type": subject_type,
            "subject": subject,
            "source_count": int(row.get("source_count") or 0),
            "latest_date": row.get("latest_date"),
            "topic_score": score,
            "recent_window_hours": config.since_hours,
        })
    targets.sort(key=lambda x: (x["topic_score"], x["source_count"], x.get("latest_date") or ""), reverse=True)
    if config.limit:
        targets = targets[:config.limit]
    return targets


def _result_label(result: dict) -> str:
    source = result.get("source_path") or result.get("url") or ""
    title = result.get("title") or Path(source).name or "untitled"
    publisher = (result.get("metadata") or {}).get("publisher") or result.get("publisher")
    return " - ".join(str(x) for x in (publisher, title) if x)


def _kb_context(subject: str, *, max_items: int = 10, max_chars: int = 1800) -> str:
    query = (
        f"{subject} business model products customers competitive landscape peers "
        f"market share orders revenue growth gross margin operating margin price target "
        f"bull case bear case risks catalysts AI semiconductor memory WFE SPE"
    )
    blocks = []
    for i, result in enumerate(kb.search(query, sources="all", limit=max_items, use_vector=False), start=1):
        blocks.append(
            f"<kb_source index='{i}' source='{_result_label(result)}'>\n"
            f"{(result.get('text') or '')[:max_chars]}\n"
            f"</kb_source>"
        )
    return "\n\n".join(blocks)


def _prompt_for_target(target: dict[str, Any]) -> str:
    from scripts.research_memory import query_context, subject_snapshot

    subject = target["subject"]
    structured = query_context(
        f"{subject} business model competitive landscape products customers market share "
        f"orders revenue growth gross margin operating margin price targets assumptions "
        f"bull case bear case risks catalysts",
        limit=28,
    )
    snapshot = subject_snapshot(subject, limit=16)
    private_context = _kb_context(subject)
    return f"""\
Study target:
- Type: {target.get('subject_type')}
- Subject: {subject}
- Source count: {target.get('source_count')}
- Latest source date: {target.get('latest_date') or 'n.d.'}

Structured research memory:
{structured or 'No structured research memory found.'}

Subject snapshot:
{snapshot or 'No subject snapshot found.'}

Private KB excerpts:
{private_context or 'No KB excerpts found.'}

Create a durable PM-grade study dossier for this target. This is for future
retrieval, not for Telegram chat, so be specific and structured.

Required format:

# {subject} - Study Dossier

## Business Profile
Explain what the company/theme actually is, what products or segments matter,
who the customers are, and what drives revenue and margins. If the available
research does not identify the business clearly, state the limitation.

## Competitive Landscape
Identify competitors, substitutes, upstream/downstream dependencies, customer
power, supplier power, and whether this target is best-in-class, a follower, or
a derivative exposure.

## Current Research Consensus
Describe what JPM, sellside, Substack authors, company materials, and user notes
say, naming the specific source/author whenever available.

## Key Assumptions And Numbers
List the important assumptions and numeric data points: price/ASP, volume/bit
growth, market share, orders, design wins, revenue growth, gross margin,
operating margin, capex, shareholder return, and price targets. Include periods
and units.

## Bull Case
Explain the strongest investment argument in complete sentences.

## Bear Case And Risks
Explain the strongest counterargument and what would invalidate the bull case.

## What Changed
Describe changes versus prior assumptions, prior notes, company guidance, or
consensus where the context identifies a delta.

## What To Watch Next
List datapoints, earnings-call questions, catalysts, and contradictory evidence.

## Open Questions
List missing facts that future study should fill.

Rules:
- Use full sentences in every bullet.
- Attribute claims to the source/author/publisher when available.
- Do not invent facts not present in the supplied memory.
- If source coverage is thin, say exactly what is thin.
- Do not include raw chunk IDs or retrieval scores.
"""


def _call_model(prompt: str, config: StudyConfig) -> str:
    system = (
        "You are an overnight study agent for a buy-side investment research system. "
        "Your job is to turn local research memory into durable, source-attributed "
        "company/theme dossiers that make future analyst answers smarter."
    )
    full_prompt = f"{system}\n\n{prompt}"
    provider = config.provider.lower().strip()
    if provider in {"openai", "gpt"}:
        return _call_openai(
            full_prompt,
            config.model,
            config.max_output_tokens,
            timeout=config.timeout,
            max_retries=0,
            reasoning_effort=os.environ.get("STUDY_OPENAI_REASONING_EFFORT", "high"),
        )
    from scripts.analyst import _resolve_thinking

    thinking, effort = _resolve_thinking("STUDY")
    return _call_anthropic(
        full_prompt,
        config.model,
        config.max_output_tokens,
        timeout=config.timeout,
        max_retries=0,
        thinking=thinking,
        effort=effort,
    )


def _write_and_index_dossier(target: dict[str, Any], text: str, config: StudyConfig) -> dict[str, Any]:
    DOSSIER_DIR.mkdir(parents=True, exist_ok=True)
    subject = target["subject"]
    filename = f"{_safe_slug(target.get('subject_type') or 'target')}__{_safe_slug(subject)}.md"
    path = DOSSIER_DIR / filename
    header = (
        "<!-- generated_by: study_agent -->\n"
        f"<!-- generated_at: {_now()} -->\n"
        f"<!-- study_target: {target.get('key')} -->\n\n"
    )
    path.write_text(header + text.strip() + "\n", encoding="utf-8")
    index_result = kb.index_text(
        title=f"{subject} - Study Dossier",
        text=text,
        source_type="study",
        source_uri=f"study:{target.get('key')}",
        source_path=str(path),
        publisher="Local Study Agent",
        metadata={
            "target": subject,
            "target_type": target.get("subject_type"),
            "scope": config.scope,
            "generated_at": _now(),
        },
        entities={"tickers": [subject] if target.get("subject_type") == "ticker" else [], "themes": [subject]},
        force=True,
        embed=config.embed,
    )
    return {"path": str(path), "index": index_result}


def run_study(config: StudyConfig) -> dict[str, Any]:
    STUDY_DIR.mkdir(parents=True, exist_ok=True)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    state = _load_state()
    targets = _select_targets(config, state)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_path = RUN_DIR / f"study_run_{run_id}.json"
    stats: dict[str, Any] = {
        "run_id": run_id,
        "started_at": _now(),
        "scope": config.scope,
        "topics": list(config.topics),
        "provider": config.provider,
        "model": config.model,
        "max_cost": config.max_cost,
        "estimated_cost": 0.0,
        "planned_targets": len(targets),
        "studied": 0,
        "skipped_budget": 0,
        "failed": 0,
        "dry_run": config.dry_run,
        "items": [],
    }
    run_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    for target in targets:
        prompt = _prompt_for_target(target)
        input_tokens = _estimate_tokens(prompt)
        output_tokens = config.max_output_tokens
        estimated = _cost_estimate_usd(
            input_tokens,
            output_tokens,
            provider=config.provider,
            model=config.model,
        )
        if config.max_cost and stats["estimated_cost"] + estimated > config.max_cost:
            stats["skipped_budget"] += 1
            stats["stop_reason"] = "max_cost"
            break

        item = {
            "target": target,
            "input_tokens_est": input_tokens,
            "output_tokens_reserved": output_tokens,
            "cost_estimate": round(estimated, 4),
            "started_at": _now(),
        }
        if config.dry_run:
            item["status"] = "planned"
            stats["items"].append(item)
            stats["estimated_cost"] = round(stats["estimated_cost"] + estimated, 4)
            continue

        try:
            text = _call_model(prompt, config)
            written = _write_and_index_dossier(target, text, config)
            item.update({
                "status": "studied",
                "finished_at": _now(),
                "chars": len(text),
                "path": written["path"],
            })
            state.setdefault("studied", {})[target["key"]] = {
                "studied_at": item["finished_at"],
                "path": written["path"],
                "cost_estimate": item["cost_estimate"],
                "scope": config.scope,
                "model": config.model,
            }
            _save_state(state)
            stats["studied"] += 1
            stats["estimated_cost"] = round(stats["estimated_cost"] + estimated, 4)
        except Exception as e:
            item.update({
                "status": "failed",
                "finished_at": _now(),
                "error": f"{type(e).__name__}: {e}",
            })
            stats["failed"] += 1
            # Brief cool-down so transient provider failures do not spin hot.
            time.sleep(5)
        stats["items"].append(item)
        run_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    stats["finished_at"] = _now()
    state.setdefault("runs", []).append({
        "run_id": run_id,
        "finished_at": stats["finished_at"],
        "studied": stats["studied"],
        "failed": stats["failed"],
        "estimated_cost": stats["estimated_cost"],
        "path": str(run_path),
    })
    _save_state(state)
    run_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return stats
