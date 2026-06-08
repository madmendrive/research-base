"""PM-grade analyst layer over the local research KB.

The KB is private memory and retrieval plumbing. This module is the user-facing
analyst voice: evaluate tickers/sectors, compare new information against prior
research, and produce investment implications without dumping chunks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from scripts import kb


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


def _call_openai(prompt: str, model: str, max_tokens: int) -> str:
    from openai import OpenAI
    from scripts.llm_provider import _openai_text

    timeout = float(os.environ.get("ANALYST_TIMEOUT", "600"))
    client = OpenAI(timeout=timeout, max_retries=int(os.environ.get("ANALYST_PROVIDER_MAX_RETRIES", "1")))
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
            "effort": os.environ.get("ANALYST_OPENAI_REASONING_EFFORT")
            or os.environ.get("OPENAI_REASONING_EFFORT")
            or "high"
        }
    with client.responses.stream(**kwargs) as stream:
        response = stream.get_final_response()
    text = _openai_text(response).strip()
    if not text:
        status = getattr(response, "status", None)
        details = getattr(response, "incomplete_details", None)
        raise RuntimeError(f"OpenAI returned no output text; status={status}, details={details}")
    return text


def _call_anthropic(prompt: str, model: str, max_tokens: int) -> str:
    from anthropic import Anthropic

    timeout = float(os.environ.get("ANALYST_TIMEOUT", "600"))
    client = Anthropic(timeout=timeout, max_retries=int(os.environ.get("ANALYST_PROVIDER_MAX_RETRIES", "1")))
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=ANALYST_SYSTEM_PROMPT,
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


def _label(result: dict) -> str:
    path = result.get("source_path") or result.get("url") or ""
    title = result.get("title") or (Path(path).name if path else "untitled")
    return f"{result.get('source_type', 'source')}: {title}"


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
    results = kb.search(_query_expansion(question), sources=sources, limit=limit)
    try:
        from scripts.research_memory import query_context

        structured_context = query_context(question, limit=14)
    except Exception:
        structured_context = ""
    try:
        from scripts.learning import learning_context

        adaptive_context = learning_context(question)
    except Exception:
        adaptive_context = ""
    if not results:
        if structured_context:
            prompt = f"""\
User query:
{question}

Adaptive analyst learning memory:
{adaptive_context or "No adaptive learning memory yet."}

Structured research memory:
{structured_context}

Produce the answer as the analyst described in the system instructions. The user
cares about investment judgment, deltas versus existing beliefs, and implications
for stocks/themes. Do not append a raw source list.
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

Private local research / knowledge base context:
{_private_context(results)}

Produce the answer as the analyst described in the system instructions. The user
cares about investment judgment, deltas versus existing beliefs, and implications
for stocks/themes. Do not append a raw source list. Do not say you are using
"chunks" or "retrieval".
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

Private local research / knowledge base context:
{_private_context(context, max_chars_per_item=1400)}

Structured research memory:
{structured_context or "No structured research-memory hits yet."}

Strict rules:
- Do not analyse other headlines mentioned in the KB context. Use them only as comparison/background.
- Do not write "#1", "#2", or a multi-headline ranked read-through.
- The output should be concise but investment-useful.

Write this Telegram-ready structure:
1. Bottom line
2. What changed
3. Compare vs existing KB
4. Implications for covered stocks/themes
5. What to watch next

Do not append a raw source list.
"""
        return _call_claude(prompt, max_tokens=2200)

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
