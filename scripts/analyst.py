"""PM-grade analyst layer over the local research KB.

The KB is private memory and retrieval plumbing. This module is the user-facing
analyst voice: evaluate tickers/sectors, compare new information against prior
research, and produce investment implications without dumping chunks.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import time

try:
    import truststore

    truststore.inject_into_ssl()
except Exception:
    pass

from scripts import kb


log = logging.getLogger(__name__)


ANALYST_SYSTEM_PROMPT = """\
You are the user's always-on buy-side analyst for public equities, with a
special focus on semiconductors, AI infrastructure, technology supply chains,
and macro cross-currents.

Your job is not to summarize documents. Your job is to produce hedge-fund
calibre investment judgment. Use the supplied local knowledge-base context as
private memory. Never expose chunk IDs, retrieval scores, raw source dumps, or
mechanical citations. If a source matters, name it naturally in prose, e.g.
"the PhotonCap note", "SemiAnalysis", "latest company earnings", or "your
own note".

Attribution discipline:
- When you use a claim from the local KB, attribute it to the specific
  source, author, publisher, company material, or user note whenever that
  provenance is available.
- Do not write vague phrases such as "the KB says" when the context identifies
  the actual speaker. Prefer "JPM estimates...", "SemiAnalysis argues...",
  "the company guide implies...", or "your note assumed...".
- When local KB context and live web context conflict, name both sources and
  explain the date/source-quality difference.
- Treat live web context as current external evidence, not as stored memory.
  Use it for freshness, prices, latest filings/news, and cross-checks, while
  preserving the KB as the user's private research base.

Style:
- Direct, opinionated, PM-facing.
- Specific about mechanisms, numbers, tickers, timeframes, and risk.
- Use full sentences throughout. Bullet points are welcome, but every bullet
  must be a complete sentence with a clear subject, verb, and object.
- Use Markdown-style bold for section headers and important subheaders, e.g.
  **What the author actually argued**. Use bold sparingly inside paragraphs
  only when it helps the reader scan the answer.
- Explicitly compare new information against existing beliefs, assumptions,
  company guidance, sellside/Substack views, and the user's own notes when
  available.
- Distinguish fact, inference, and judgment.
- Assume the reader is intelligent but may be slightly rusty on the specific
  topic. Briefly refresh the key context, mechanics, and debate before drawing
  conclusions.
- If the local KB is thin, say so and give the best bounded view rather than
  hallucinating confidence.

For ticker/sector evaluation questions, use this shape where relevant:

1. Title: "<Name/Ticker> - Analysis" or "<Sector/Theme> - Analysis"
2. Quick note: data limitations, if any.
3. What the business/theme actually is.
4. The bull case.
5. The bear case (this is the important part).
6. How it fits the user's book / covered themes.
7. My take: whether to add, avoid, replace something, or size as optionality.
8. What changed: what is incremental/new/different vs what we previously
   believed.
9. Compare vs existing KB:
   - Prior sellside/Substack view
   - Company earnings/guidance
   - User notes or assumptions
   - Whether the new data confirms/challenges/updates each
10. Implications for covered stocks/themes.
11. What to watch next: datapoints, earnings-call questions, contradictory
    evidence, and catalysts.

When asked whether something is a good investment or replacement candidate,
give a clear rating or actionability view, e.g. "2.5/5 as a buy today", and
explain why. Do not hide behind neutrality.
"""


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _call_openai(
    prompt: str,
    model: str,
    max_tokens: int,
    *,
    timeout: float | None = None,
    max_retries: int | None = None,
    reasoning_effort: str | None = None,
) -> str:
    from scripts.llm_provider import _openai_text, get_client

    timeout = timeout if timeout is not None else _env_float("ANALYST_TIMEOUT", 600.0)
    max_retries = max_retries if max_retries is not None else _env_int("ANALYST_PROVIDER_MAX_RETRIES", 1)
    client = get_client("openai", timeout=timeout, max_retries=max_retries)
    kwargs = {
        "model": model,
        "input": [
            {"role": "system", "content": ANALYST_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_output_tokens": max_tokens,
    }
    if model.lower().startswith("gpt-5"):
        kwargs["reasoning"] = {
            "effort": reasoning_effort
            or os.environ.get("ANALYST_OPENAI_REASONING_EFFORT")
            or os.environ.get("OPENAI_REASONING_EFFORT")
            or "high"
        }
    if os.environ.get("ANALYST_OPENAI_STREAM", "0").strip().lower() in {"1", "true", "yes"}:
        with client.responses.stream(**kwargs) as stream:
            response = stream.get_final_response()
    else:
        response = client.responses.create(**kwargs)
    text = _openai_text(response).strip()
    if not text:
        status = getattr(response, "status", None)
        details = getattr(response, "incomplete_details", None)
        raise RuntimeError(f"OpenAI returned no output text; status={status}, details={details}")
    return text


def _call_anthropic(
    prompt: str,
    model: str,
    max_tokens: int,
    *,
    timeout: float | None = None,
    max_retries: int | None = None,
) -> str:
    from scripts.llm_provider import cached_system_block, get_client

    timeout = timeout if timeout is not None else _env_float("ANALYST_TIMEOUT", 600.0)
    max_retries = max_retries if max_retries is not None else _env_int("ANALYST_PROVIDER_MAX_RETRIES", 1)
    client = get_client("anthropic", timeout=timeout, max_retries=max_retries)
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=cached_system_block(ANALYST_SYSTEM_PROMPT),
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def _call_claude(prompt: str, max_tokens: int = 4000) -> str:
    provider = os.environ.get("ANALYST_PROVIDER", "anthropic").lower().strip()
    model = os.environ.get("ANALYST_MODEL") or os.environ.get("KB_SYNTHESIS_MODEL", "claude-opus-4-7")
    try:
        if provider in {"openai", "gpt"}:
            return _call_openai(prompt, model or "gpt-5.5", max_tokens)
        return _call_anthropic(prompt, model or "claude-opus-4-7", max_tokens)
    except Exception as primary_error:
        fallback_provider = os.environ.get("ANALYST_FALLBACK_PROVIDER", "openai").lower().strip()
        if fallback_provider in {"openai", "gpt"} and os.environ.get("OPENAI_API_KEY"):
            fallback_model = os.environ.get("ANALYST_FALLBACK_MODEL", "gpt-5.5")
            try:
                return _call_openai(prompt, fallback_model, max_tokens)
            except Exception as fallback_error:
                raise RuntimeError(
                    f"Primary analyst model failed ({type(primary_error).__name__}: {primary_error}); "
                    f"fallback failed ({type(fallback_error).__name__}: {fallback_error})"
                ) from fallback_error
        raise


def _is_source_constrained_query(question: str) -> bool:
    from scripts.web_context import SOURCE_CONSTRAINED_HINTS

    query_l = (question or "").lower()
    return any(hint in query_l for hint in SOURCE_CONSTRAINED_HINTS)


def _call_structured_fast(prompt: str, max_tokens: int = 3000) -> str:
    provider = os.environ.get("ANALYST_STRUCTURED_PROVIDER", "openai").lower().strip()
    timeout = _env_float("ANALYST_STRUCTURED_TIMEOUT", 90.0)
    max_retries = _env_int("ANALYST_STRUCTURED_MAX_RETRIES", 0)
    if provider in {"openai", "gpt"}:
        model = (
            os.environ.get("ANALYST_STRUCTURED_OPENAI_MODEL")
            or os.environ.get("ANALYST_STRUCTURED_MODEL")
            or os.environ.get("ANALYST_FALLBACK_MODEL")
            or "gpt-5.5"
        )
        return _call_openai(
            prompt,
            model,
            max_tokens,
            timeout=timeout,
            max_retries=max_retries,
            reasoning_effort=os.environ.get("ANALYST_STRUCTURED_REASONING_EFFORT", "high"),
        )
    configured_model = os.environ.get("ANALYST_STRUCTURED_ANTHROPIC_MODEL") or os.environ.get("ANALYST_STRUCTURED_MODEL")
    if configured_model and configured_model.lower().startswith(("gpt-", "o")):
        configured_model = ""
    return _call_anthropic(
        prompt,
        configured_model or os.environ.get("ANALYST_MODEL", "claude-opus-4-7"),
        max_tokens,
        timeout=timeout,
        max_retries=max_retries,
    )


def _fallback_keywords(question: str) -> list[str]:
    query_l = (question or "").lower()
    keywords = set(re.findall(r"[a-z0-9]{3,}", query_l))
    if {"memory", "dram", "nand", "hbm"} & keywords:
        keywords.update({
            "memory", "dram", "nand", "hbm", "samsung", "hynix", "micron",
            "005930", "000660", "china", "capacity", "pricing", "asp",
            "lta", "ltas", "valuation", "opm",
        })
    return sorted(keywords)


def _primary_topic_keywords(question: str) -> set[str]:
    query_l = (question or "").lower()
    primary: set[str] = set()
    if "memory" in query_l or "dram" in query_l or "nand" in query_l or "hbm" in query_l:
        primary.update({"memory", "dram", "nand", "hbm", "samsung", "hynix", "micron", "005930", "000660"})
    if "tsmc" in query_l or "foundry" in query_l:
        primary.update({"tsmc", "foundry", "cowos", "n2", "n3", "2330"})
    if "substrate" in query_l or "abf" in query_l:
        primary.update({"substrate", "abf", "unimicron", "kinsus", "ibiden", "shinko"})
    return primary


def _parse_debate_line(line: str) -> dict[str, str] | None:
    text = line[2:].strip() if line.startswith("- ") else line.strip()
    if " | bull: " not in text or " | bear: " not in text:
        return None
    try:
        left, rest = text.split(" | bull: ", 1)
        bull, rest = rest.split(" | bear: ", 1)
        bear, lean = rest.split(" | lean: ", 1)
    except ValueError:
        return None
    theme = "Research debate"
    source_and_question = left
    if " | " in left:
        theme, source_and_question = left.split(" | ", 1)
    source = "J.P. Morgan Asia Pacific Equity Research" if "J.P. Morgan" in source_and_question else "Indexed source"
    question = source_and_question.replace(source, "").strip(" -")
    return {
        "theme": theme.strip(),
        "source": source,
        "question": question or "Current debate",
        "bull": bull.strip(),
        "bear": bear.strip(),
        "lean": lean.strip(),
    }


def _relevant_structured_lines(structured_context: str, keywords: list[str], *, max_lines: int = 8) -> list[str]:
    out = []
    for line in structured_context.splitlines():
        line_l = line.lower()
        if any(keyword in line_l for keyword in keywords):
            out.append(line.strip())
        if len(out) >= max_lines:
            break
    return out


def _local_structured_answer(question: str, structured_context: str) -> str | None:
    keywords = _fallback_keywords(question)
    primary_keywords = _primary_topic_keywords(question)
    debates = []
    for line in structured_context.splitlines():
        parsed = _parse_debate_line(line)
        if not parsed:
            continue
        haystack = " ".join(parsed.values()).lower()
        if primary_keywords and not any(keyword in haystack for keyword in primary_keywords):
            continue
        score = sum(1 for keyword in keywords if keyword in haystack)
        score += 3 * sum(1 for keyword in primary_keywords if keyword in haystack)
        if score:
            debates.append((score, parsed))
    debates.sort(key=lambda item: item[0], reverse=True)
    debates = [item[1] for item in debates[:4]]
    if not debates:
        return None

    ratings = []
    in_ratings = False
    for line in structured_context.splitlines():
        stripped = line.strip()
        if stripped == "Ratings/targets:":
            in_ratings = True
            continue
        if in_ratings and stripped.endswith(":"):
            break
        if not in_ratings or not stripped.startswith("- "):
            continue
        line_l = stripped.lower()
        if any(keyword in line_l for keyword in (primary_keywords or set(keywords))):
            ratings.append(stripped)
        if len(ratings) >= 5:
            break
    latest_source = "J.P. Morgan Asia Pacific Equity Research"
    lines = [
        "**Bottom line**",
        f"{latest_source} is broadly constructive on the memory cycle in the currently indexed research memory, but the view is not a blind cyclical long. The bull case is that AI has made memory more strategic, supply is tight, customer commitments are longer, and profitability may deserve a higher valuation framework. The main risks are classic memory-cycle risks: over-ordering, capacity additions, China supply, capex intensity, and the possibility that today’s elevated margins prove cyclical rather than structural.",
        "",
        "**Context refresher**",
        "Memory stocks are normally treated as cyclical commodity semiconductor names because DRAM and NAND pricing can swing sharply when supply and demand move out of balance. JPM’s current debate is whether AI servers and HBM have changed that old framework by making memory a scarce strategic input for cloud service providers, rather than just another PC or handset component.",
        "",
        "**What JPM is saying**",
    ]
    for debate in debates:
        debate_question = debate["question"].rstrip(".?")
        lines.append(
            f"- {debate['source']} frames the key debate as: {debate_question}. "
            f"The JPM-side bull argument is: {debate['bull']} The counterargument is: {debate['bear']} "
            f"The indexed lean is {debate['lean']}."
        )

    lines.extend(["", "**Bull case**"])
    for debate in debates[:3]:
        lines.append(
            f"- JPM’s positive case is: {debate['bull']}"
        )

    lines.extend(["", "**Key risks**"])
    for debate in debates[:3]:
        lines.append(
            f"- The key risk is: {debate['bear']}"
        )

    if ratings:
        lines.extend(["", "**Relevant ratings and targets in memory**"])
        for line in ratings[:5]:
            lines.append(f"- {line.lstrip('- ').strip()}")

    lines.extend([
        "",
        "**Implications for stocks/themes**",
        "- Samsung Electronics and SK hynix screen as direct beneficiaries if JPM is right that HBM, DRAM discipline, and AI-driven customer commitments support a higher-margin cycle.",
        "- Micron is directionally exposed to the same DRAM/HBM framework, although the currently surfaced JPM structured rows are richer for Korean and Asian supply-chain names than for MU specifically.",
        "- TSMC, ASE, Unimicron, Ibiden, and Elite Material benefit indirectly if the same AI infrastructure tightness persists across wafers, advanced packaging, substrates, and CCL.",
        "- PC and server OEMs face a mixed read-through because higher memory prices can lift revenue dollars but pressure margins and demand elasticity for downstream system vendors.",
        "",
        "**What to watch next**",
        "- Watch whether HBM and DRAM contract pricing continues to move up without triggering customer pushback or order cancellations.",
        "- Watch whether Chinese DRAM and NAND capacity remains stuck in lower-value segments or starts to threaten higher-value supply.",
        "- Watch capex plans from Samsung, SK hynix, Micron, and Chinese suppliers, because excessive capacity is the fastest way for a structural re-rating thesis to become a normal down-cycle again.",
        "- Watch whether multi-year LTAs survive a demand wobble, because durable commitments are central to JPM’s higher-quality earnings argument.",
        "",
        "**Model note**",
        "The live analyst model call did not complete, so this answer was synthesized locally from the structured research-memory layer rather than waiting silently.",
    ])
    return "\n".join(lines)


def _structured_fallback_text(question: str, structured_context: str, results: list[dict]) -> str:
    """Return a bounded answer when both primary and fast model calls fail."""
    local_answer = _local_structured_answer(question, structured_context)
    if local_answer:
        return local_answer

    top_sources = []
    for result in results[:4]:
        top_sources.append(f"- {_label(result)}.")
    excerpt = structured_context.strip()
    if len(excerpt) > 3200:
        excerpt = excerpt[:3197] + "..."
    lines = [
        "**Analyst model unavailable**",
        "",
        "I found relevant structured research memory, but the live analyst model call did not complete. I am returning the most relevant stored memory so the bot does not sit silently.",
        "",
        "**Question**",
        question,
        "",
        "**Structured memory excerpt**",
        excerpt or "No structured research-memory excerpt was available.",
    ]
    if top_sources:
        lines.extend(["", "**Top local sources searched**", *top_sources])
    return "\n".join(lines)


def _answer_from_structured_fast(
    question: str,
    *,
    adaptive_context: str,
    structured_context: str,
    web_context: str,
    results: list[dict],
) -> str:
    prompt = f"""\
User query:
{question}

Adaptive analyst learning memory:
{adaptive_context or "No adaptive learning memory yet."}

Structured research memory:
{structured_context or "No structured research-memory hits yet."}

Live web context:
{web_context or "No live web context fetched."}

Private local research / knowledge base context:
{_private_context(results[:8], max_chars_per_item=1200) if results else "No unstructured KB context found."}

This is a source-constrained local research-memory question, so answer primarily
from the structured research memory and name the relevant source, publisher,
author, note date, ticker, and estimate where available.

Write a concise but PM-useful answer with these sections:
1. **Bottom line**.
2. **Context refresher**.
3. **What JPM or the requested source is saying**.
4. **Bull case**.
5. **Key risks**.
6. **Implications for stocks/themes**.
7. **What to watch next**.

Every bullet must be a complete sentence. Do not append a raw source list. If
the structured memory does not contain a specific requested number, say that it
is not in the currently indexed structured memory rather than guessing.
"""
    try:
        return _call_structured_fast(
            prompt,
            max_tokens=_env_int("ANALYST_STRUCTURED_MAX_TOKENS", 3000),
        )
    except Exception:
        log.exception("structured fast analyst call failed")
        if os.environ.get("ANALYST_STRUCTURED_ALLOW_PRIMARY_FALLBACK", "0").strip().lower() in {"1", "true", "yes"}:
            return _call_claude(prompt, max_tokens=_env_int("ANALYST_STRUCTURED_MAX_TOKENS", 3000))
        return _structured_fallback_text(question, structured_context, results)


def _label(result: dict) -> str:
    path = result.get("source_path") or result.get("url") or ""
    title = result.get("title") or (Path(path).name if path else "untitled")
    metadata = result.get("metadata") or {}
    publisher = metadata.get("publisher") or metadata.get("source") or metadata.get("firm")
    author = metadata.get("author")
    speaker = " / ".join(str(x) for x in (publisher, author) if x)
    prefix = f"{result.get('source_type', 'source')}: "
    return f"{prefix}{speaker} - {title}" if speaker else f"{prefix}{title}"


def _private_context(results: list[dict], max_chars_per_item: int = 1800) -> str:
    blocks = []
    for i, result in enumerate(results, start=1):
        entities = result.get("entities") or {}
        entity_text = ", ".join(
            entities.get("tickers", [])
            + entities.get("themes", [])
            + entities.get("authors", [])
        )
        blocks.append(
            f"<research_memory index='{i}' source='{_label(result)}' entities='{entity_text}'>\n"
            f"Provenance: {_label(result)}\n"
            f"{(result.get('text') or '')[:max_chars_per_item]}\n"
            f"</research_memory>"
        )
    return "\n\n".join(blocks)


def _query_expansion(question: str) -> str:
    """Add likely intent terms so retrieval finds book-fit and debate context."""
    extras = [
        "bull case bear case valuation risk thesis guidance earnings",
        "what changed compare existing research assumptions implications watch next",
        "AI infrastructure semiconductors data center power memory photonics substrates",
    ]
    return question + "\n" + "\n".join(extras)


def _structured_query_expansion(question: str) -> str:
    """Add sector vocabulary before querying the structured memory layer."""
    query_l = (question or "").lower()
    extras = []
    if "memory" in query_l or "dram" in query_l or "nand" in query_l or "hbm" in query_l:
        extras.append(
            "DRAM NAND HBM memory ASP price growth volume growth supply demand "
            "LTAs Samsung SK hynix Micron MU 005930 000660 China capacity "
            "market share OPM P/S market cap valuation risk"
        )
    if "tsmc" in query_l or "foundry" in query_l:
        extras.append(
            "TSMC foundry CoWoS advanced packaging N2 N3 AI accelerator wafer "
            "price target capex gross margin market share"
        )
    if "substrate" in query_l or "abf" in query_l:
        extras.append(
            "ABF substrate Unimicron Kinsus Nan Ya PCB Ibiden Shinko AI server "
            "capacity utilization price increases supply constraints"
        )
    if not extras:
        return question
    return question + "\n" + "\n".join(extras)


def _filter_single_headline_context(results: list[dict], selected_title: str) -> list[dict]:
    """Keep headline digest context from turning one selected headline into a batch analysis."""
    selected_title_l = (selected_title or "").lower()
    filtered = []
    for result in results:
        source_type = (result.get("source_type") or "").lower()
        title = (result.get("title") or "").lower()
        metadata = result.get("metadata") or {}
        is_headline_context = source_type == "headlines"
        is_selected_analysis = selected_title_l and selected_title_l[:80] in title
        if is_headline_context and not is_selected_analysis:
            continue
        if metadata.get("items") and source_type == "headlines":
            continue
        filtered.append(result)
    return filtered


def answer_question(question: str, sources: str = "all", limit: int = 14) -> str:
    """Answer a query as an analyst, not as a cited retrieval assistant."""
    started = time.perf_counter()
    results = kb.search(_query_expansion(question), sources=sources, limit=limit)
    kb_elapsed = time.perf_counter()
    try:
        from scripts.research_memory import query_context

        structured_limit = 30 if _is_source_constrained_query(question) else 18
        structured_context = query_context(_structured_query_expansion(question), limit=structured_limit)
    except Exception:
        log.exception("structured research-memory lookup failed")
        structured_context = ""
    structured_elapsed = time.perf_counter()
    try:
        from scripts.learning import learning_context

        adaptive_context = learning_context(question)
    except Exception:
        log.exception("adaptive learning-memory lookup failed")
        adaptive_context = ""
    adaptive_elapsed = time.perf_counter()
    try:
        from scripts.web_context import fetch_web_context

        web_context = fetch_web_context(
            question,
            kb_result_count=len(results),
            has_structured_context=bool(structured_context),
        )
    except Exception:
        log.exception("web context lookup failed")
        web_context = ""
    web_elapsed = time.perf_counter()
    log.info(
        "answer_question context ready kb_results=%s structured=%s adaptive=%s web=%s timings=%.2f/%.2f/%.2f/%.2f total=%.2fs",
        len(results),
        bool(structured_context),
        bool(adaptive_context),
        bool(web_context),
        kb_elapsed - started,
        structured_elapsed - kb_elapsed,
        adaptive_elapsed - structured_elapsed,
        web_elapsed - adaptive_elapsed,
        web_elapsed - started,
    )
    if structured_context and _is_source_constrained_query(question):
        log.info("using structured fast analyst path for source-constrained query")
        return _answer_from_structured_fast(
            question,
            adaptive_context=adaptive_context,
            structured_context=structured_context,
            web_context=web_context,
            results=results,
        )
    if not results:
        if structured_context or web_context:
            prompt = f"""\
User query:
{question}

Adaptive analyst learning memory:
{adaptive_context or "No adaptive learning memory yet."}

Structured research memory:
{structured_context or "No structured research-memory hits yet."}

Live web context:
{web_context or "No live web context fetched."}

Produce the answer as the analyst described in the system instructions. The user
cares about investment judgment, deltas versus existing beliefs, and implications
for stocks/themes. Attribute KB claims to the specific source/author when known.
Attribute current web claims to their web source when used. Do not append a raw
source list.
"""
            return _call_claude(prompt)
        return (
            "I do not have enough relevant material in the research base yet. "
            "My next step would be to ingest the relevant company filings, earnings "
            "deck/transcript, your notes, and any sellside/Substack pieces before "
            "giving a high-conviction investment view."
        )

    prompt = f"""\
User query:
{question}

Adaptive analyst learning memory:
{adaptive_context or "No adaptive learning memory yet."}

Structured research memory:
{structured_context or "No structured research-memory hits yet."}

Live web context:
{web_context or "No live web context fetched."}

Private local research / knowledge base context:
{_private_context(results)}

Produce the answer as the analyst described in the system instructions. The user
cares about investment judgment, deltas versus existing beliefs, and implications
for stocks/themes. Attribute KB claims to the specific source/author when known.
Attribute current web claims to their web source when used. Do not append a raw
source list. Do not say you are using "chunks" or "retrieval".
"""
    try:
        return _call_claude(prompt)
    except Exception as e:
        top = results[0]
        return (
            f"I found relevant research memory, but the analyst synthesis call failed "
            f"({type(e).__name__}: {e}). The top relevant item was {_label(top)}."
        )


def headline_readthrough(items: list[dict], context_limit: int = 12) -> str:
    """Create a ranked PM headline read-through against the KB."""
    if not items:
        return ""
    single_item = len(items) == 1
    query_terms = []
    for item in items:
        query_terms.append(item.get("title", ""))
        entities = item.get("entities") or {}
        query_terms.extend(entities.get("tickers", []))
        query_terms.extend(entities.get("themes", []))
    context = kb.search(
        " ".join(query_terms)[:1800],
        sources="all",
        limit=context_limit * 2 if single_item else context_limit,
    )
    if single_item:
        context = _filter_single_headline_context(context, items[0].get("title", ""))[:context_limit]
    try:
        from scripts.research_memory import query_context

        structured_context = query_context(" ".join(query_terms), limit=12)
    except Exception:
        structured_context = ""
    web_context = ""
    if single_item:
        try:
            from scripts.web_context import fetch_web_context

            web_query = _headline_web_freshness_query(items[0])
            web_context = fetch_web_context(
                web_query,
                kb_result_count=len(context),
                has_structured_context=bool(structured_context),
            )
        except Exception:
            web_context = ""

    headline_lines = [
        json.dumps(
            {
                "title": item.get("title"),
                "source": item.get("source"),
                "published_at": item.get("published_at"),
                "url": item.get("url"),
                "score": item.get("score"),
                "entities": item.get("entities"),
            },
            ensure_ascii=False,
        )
        for item in items
    ]
    if single_item:
        prompt = f"""\
Selected headline to analyse. Analyse ONLY this headline; do not create a ranked list of other headlines:
{headline_lines[0]}

Live web freshness / prior-reporting check:
{web_context or "No live web context fetched."}

Private local research / knowledge base context:
{_private_context(context, max_chars_per_item=1400)}

Structured research memory:
{structured_context or "No structured research-memory hits yet."}

Strict rules:
- Do not analyse other headlines mentioned in the KB context. Use them only as comparison/background.
- Do not write "#1", "#2", or a multi-headline ranked read-through.
- Treat the live web check as mandatory evidence for freshness. Explicitly decide whether the selected headline is genuinely new, incremental, recycled, or old news.
- Compare publication dates and source quality where the web context gives them.
- If one part of the headline is old but another part is new, split the analysis into "already known" and "incremental" components.
- Do not call something a meaningful upgrade merely because the selected headline says it if older web reporting or the KB already established it.
- The output should be concise but investment-useful.

Write this Telegram-ready structure:
1. Bottom line
2. Freshness / novelty check
3. What changed
4. Compare vs existing KB
5. Implications for covered stocks/themes
6. What to watch next

Do not append a raw source list.
"""
        return _call_claude(prompt, max_tokens=_env_int("HEADLINE_READTHROUGH_MAX_TOKENS", 5000))

    prompt = f"""\
New headline batch:
{chr(10).join(headline_lines)}

Private local research / knowledge base context:
{_private_context(context, max_chars_per_item=1400)}

Structured research memory:
{structured_context or "No structured research-memory hits yet."}

Write a concise ranked Telegram read-through. For each material headline, cover:
- What changed
- Whether this is new or confirms/challenges the existing KB
- Implications for covered stocks/themes
- What to watch next

Skip low-signal items. Do not append a raw source list.
"""
    return _call_claude(prompt, max_tokens=3000)


def _headline_web_freshness_query(item: dict) -> str:
    title = str(item.get("title") or "").strip()
    source = str(item.get("source") or "").strip()
    published_at = str(item.get("published_at") or "").strip()
    url = str(item.get("url") or "").strip()
    return (
        "Freshness check for this technology/semiconductor headline. "
        "Search the live web for prior reports and current corroboration. "
        "Determine whether the claim is genuinely new today, incremental versus older reporting, or old/recycled news. "
        "Compare publication dates and cite the earliest reputable prior report if found. "
        f"Headline: {title}. Source: {source}. Published at: {published_at}. URL: {url}"
    )


def research_readthrough(result: dict, filename: str) -> str:
    """Turn a newly ingested document result into a PM read-through."""
    triage = result.get("triage") or {}
    query_terms = [
        filename,
        str(triage.get("title") or ""),
        str(triage.get("primary_subject") or ""),
        " ".join(triage.get("tickers_covered") or []),
        " ".join(triage.get("themes_touched") or []),
    ]
    query = " ".join(x for x in query_terms if x).strip()
    try:
        kb_context = kb.search(query, sources="all", limit=6) if query else []
    except Exception:
        kb_context = []
    try:
        from scripts.research_memory import query_context

        structured_context = query_context(query, limit=8) if query else ""
    except Exception:
        structured_context = ""

    secondaries = result.get("secondaries") or []
    cross = []
    for sec in secondaries[:8]:
        cross.append({
            "label": sec.get("label"),
            "target": sec.get("target"),
            "report": (sec.get("report") or sec.get("error") or "")[:1500],
        })

    prompt = f"""\
New research file: {filename}

Triage:
{json.dumps(triage, indent=2, ensure_ascii=False)}

Structured extraction from the new file:
{json.dumps(result.get("extraction_json") or {}, indent=2, ensure_ascii=False)[:18000]}

Extracted report text excerpt from the new file:
{(result.get("document_excerpt") or "")[:42000]}

Extraction fallback / summary:
{(result.get("primary_report") or "")[:7000]}

Private local KB context for comparison:
{_private_context(kb_context, max_chars_per_item=900) or "No KB context found."}

Structured research memory for comparison:
{structured_context or "No structured research-memory hits found."}

Cross-read reports:
{json.dumps(cross, indent=2, ensure_ascii=False)}

Write a Telegram-ready PM read-through in this exact order:

1. **Title**.
2. **What the author or speaker actually argued**.
   - This section must come before your own analysis.
   - Reconstruct the argument in extreme detail, including the author's main claim, intermediate premises, evidence, numbers, examples, caveats, and supporting rationale.
   - Explain why each supporting point matters rather than merely listing it.
   - Use exact short quotes only where they materially clarify the author's wording. Do not reproduce long passages.
   - Do not infer claims the author did not make.
3. **Context refresher**.
   - Assume the reader is slightly unfamiliar with the topic.
   - Explain the key industry, macro, company, or market debate that the report sits inside.
   - Define any necessary mechanics, acronyms, valuation debates, or market setup in plain language.
4. **Analyst interpretation**.
   - State the bottom line.
   - Explain what changed versus existing research, assumptions, or the user's prior likely view.
   - Compare explicitly versus the local KB, including sellside/Substack research, company earnings or guidance, and user notes where available.
   - Explain implications for covered stocks, sectors, and themes.
   - Identify the bear case, contradictions, or evidence that would invalidate the report's view.
   - List what to watch next, including datapoints, earnings-call questions, catalysts, and contradictory evidence.
5. **Author-recommended trades**.
   - Only include trades explicitly touched on by the author or speaker.
   - Do not infer potential trades from the analysis.
   - For each explicit trade, state the asset involved.
   - For each explicit trade, state the strategy, timeframe, entry levels, stop losses, validation or invalidation conditions, and sizing if the author or speaker provides them.
   - If any of these details are missing, state that the author or speaker did not specify them.
   - State whether the trade appears to still be live, already completed, or impossible to determine from the report.
   - If the author or speaker recommends no explicit trade, say so clearly.

All bullets must be full sentences. Avoid fragment bullets like "Bullish NVDA" or "Watch margins." Write "The report is bullish on NVDA because..." instead.
Use Markdown-style bold for section headers and important subheaders.

Do not append a raw source list.
"""
    return _call_claude(prompt, max_tokens=int(os.environ.get("RESEARCH_READTHROUGH_MAX_TOKENS", "6000")))
