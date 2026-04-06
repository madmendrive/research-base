"""Shared analysis report generation — used by store-research, store-macro,
and store-thematic to generate the three-section analysis report:

1. Key Points & Takeaways
2. Trader's Brief
3. View Evolution & Cross-Author Comparison (generated in a second API call)
"""

ANALYSIS_REPORT_INSTRUCTIONS = """
In addition to all the structured fields above, also generate an "analysis_report" field — a long-form markdown string with THREE sections:

## Key Points & Takeaways

For each key point or argument made in the document:

- **[Point title]**: Explain the author's argument and supporting rationale in full. You are encouraged to use direct quotes from the document. Your explanation should assume the reader has little to zero knowledge of the underlying concepts, but should be sufficiently technical and structured in a manner which ramps up the reader's understanding of all necessary concepts such that they can grasp the key takeaways. Each bullet point should lead to full sentences, not fragments. Be thorough — cover every significant argument, data point, and conclusion.

## Trader's Brief

Outline any trades explicitly recommended by the author or speaker. For each trade:

- **[Asset name]**: Clearly state:
  - The asset involved in the trade
  - The trade strategy: timeframe of the trade, entry levels, stop losses, invalidating or validating conditions/circumstances, sizing
  - If the author does not touch on any of these details, state so explicitly (e.g. "Author does not specify entry levels or stop losses")
  - Whether this trade is still live or one that was already completed/closed
  - The author's apparent conviction level relative to their other trade ideas, if discernible

CRITICAL RULES for the Trader's Brief:
- Only outline trades that are EXPLICITLY touched upon by the author. Do NOT infer "potential trades" based on the content.
- If the author makes no trade recommendations, state: "No explicit trade recommendations in this document."
- Each bullet point should lead to full sentences.

## View Evolution & Cross-Author Comparison

NOTE: Leave this field as "PENDING_SECOND_PASS" — it will be populated in a subsequent step.
"""

VIEW_EVOLUTION_PROMPT = """\
You are a buy-side equity research analyst at a macro hedge fund. I'm providing:

1. NEW RESEARCH NOTE (extracted data): see below
2. AUTHOR'S PREVIOUS VIEWS WITH HISTORY: see below
3. ALL AUTHORS' CURRENT VIEWS: see below

Generate a "View Evolution & Cross-Author Comparison" section in markdown with two parts:

### How the Author's Views Have Changed

For each key topic covered in the new note, compare against the author's previous views from their history. Format as:

- **[Topic]**: Previously ([date]): "[old view]" → Now: "[new view]". [Describe the direction and magnitude of the shift. Was this a major reversal, a gradual evolution, or a minor tweak? What drove the change — new data, a change in macro conditions, a revised framework?]

- If the view hasn't meaningfully changed, state: "Unchanged from [date] — author maintains [view]."

- If this is the first note from the author on this topic, state: "New view — no prior history for comparison."

Cover every topic where the author expresses a view, not just the ones that changed.

### Comparison to Other Authors

For each key topic, show where this author stands relative to other authors in the research base. Format as:

- **[Topic]**: [Author] says "[view]". This is [aligned with / more bullish than / more bearish than / divergent from] [other author] who says "[their view]" ([date]) and [other author] who says "[their view]" ([date]).

- If there's a clear consensus, call it out: "The author is an outlier — 3 of 4 other analysts expect X while the author expects Y."

- If the author is aligned with consensus, state that too: "This is in line with the prevailing view across [X] other analysts."

- Quantify differences where possible: "Author's revenue estimate of $450B is 8% above the consensus mean of $417B."

Be extremely specific. Use actual numbers, dates, and names. Each bullet point should lead to full sentences.
"""

MACRO_TRADE_HISTORY_ADDENDUM = """
Also generate a "### Trade History Context" sub-section under the Trader's Brief. Using the author's recommended_trades_history, identify:
- Which previously recommended trades appear to still be live vs closed
- How new trade recommendations compare to previous ones
- Whether the author's overall positioning has shifted
- Track record context where observable (e.g. "Author recommended long gold at $2,100 in Jan, now at $2,350 — a profitable call")
"""


import json


def build_second_pass_prompt(new_note_json, author_history_json=None,
                             summary_json=None, trades_history=None):
    """Build the prompt for the second API call that generates View Evolution."""
    prompt = VIEW_EVOLUTION_PROMPT

    prompt += f"\n\n--- NEW RESEARCH NOTE ---\n{json.dumps(new_note_json, indent=2)[:15000]}\n"

    if author_history_json:
        history_str = json.dumps(author_history_json, indent=2)
        if len(history_str) > 10000:
            history_str = history_str[:10000] + "\n[...truncated]"
        prompt += f"\n--- AUTHOR'S PREVIOUS VIEWS ---\n{history_str}\n"
    else:
        prompt += "\n--- AUTHOR'S PREVIOUS VIEWS ---\nNo previous notes from this author.\n"

    if summary_json:
        summary_str = json.dumps(summary_json, indent=2)
        if len(summary_str) > 10000:
            summary_str = summary_str[:10000] + "\n[...truncated]"
        prompt += f"\n--- ALL AUTHORS' CURRENT VIEWS ---\n{summary_str}\n"
    else:
        prompt += "\n--- ALL AUTHORS' CURRENT VIEWS ---\nNo other authors in the research base for comparison.\n"

    if trades_history:
        prompt += MACRO_TRADE_HISTORY_ADDENDUM
        trades_str = json.dumps(trades_history, indent=2)
        if len(trades_str) > 5000:
            trades_str = trades_str[:5000] + "\n[...truncated]"
        prompt += f"\n--- AUTHOR'S TRADE HISTORY ---\n{trades_str}\n"

    return prompt


def merge_analysis_report(note_data, view_evolution_text):
    """Replace PENDING_SECOND_PASS in the analysis_report with actual content."""
    report = note_data.get("analysis_report", "")
    if "PENDING_SECOND_PASS" in report:
        report = report.replace("PENDING_SECOND_PASS", view_evolution_text)
    else:
        # Append if placeholder not found
        report += "\n\n## View Evolution & Cross-Author Comparison\n\n" + view_evolution_text
    note_data["analysis_report"] = report
    return report
