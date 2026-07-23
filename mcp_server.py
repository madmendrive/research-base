"""Local MCP server exposing the research KB to Claude Desktop (read-only).

Registered in %APPDATA%\\Claude\\claude_desktop_config.json; Claude Desktop
launches this over stdio. The desktop app's Claude (subscription-billed)
does the synthesis — these tools only retrieve. The Telegram/Discord analyst
(API-billed Fable) is unaffected and keeps working in parallel.

Tools:
  search_kb           hybrid FTS+vector search over the full corpus (~950k chunks)
  research_context    structured research-memory context for a question
  subject_snapshot    per-subject view (ticker / author / theme) from memory
  company_summary     stored research summary.md for a ticker
  theme_summary       theme_summary.md for a thematic folder
  latest_tech_brief   the most recent Tech Brief digest
"""

import truststore

truststore.inject_into_ssl()

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("research-kb")

DATA = ROOT / "data"


def _read(path: Path, max_chars: int = 60000) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:max_chars]


@mcp.tool()
def search_kb(query: str, sources: str = "all", limit: int = 8) -> str:
    """Hybrid keyword+semantic search over the entire research KB: sellside
    notes, macro/semis author letters, thematic research, IR materials, and
    regulatory filings for 226 companies. Use for any question about stored
    research, estimates, or company/theme views. sources: "all" or a
    comma-separated subset like "research,macro,headlines"."""
    from scripts import kb

    results = kb.search(query, sources=sources, limit=max(1, min(int(limit), 20)))
    if not results:
        return "No KB hits for that query."
    out = []
    for i, r in enumerate(results, start=1):
        src = r.get("source_path") or r.get("url") or "unknown"
        out.append(
            f"[{i}] {r.get('title', '(untitled)')} ({r.get('source_type')}, {src})\n"
            f"{(r.get('text') or '')[:1800]}"
        )
    return "\n\n---\n\n".join(out)


@mcp.tool()
def research_context(question: str, limit: int = 10) -> str:
    """Compact structured-memory context for a research question: recent
    sources, per-subject estimates, and view changes relevant to the query.
    Cheaper and more structured than search_kb; good first call."""
    from scripts.research_memory import query_context

    return query_context(question, limit=max(1, min(int(limit), 20))) or "No structured-memory context found."


@mcp.tool()
def subject_snapshot(subject: str) -> str:
    """Structured-memory snapshot for one subject: a ticker (e.g. '2330 TT',
    'MU'), an author (e.g. 'SemiAnalysis'), or a theme. Shows the stored
    sources and current views for that subject."""
    from scripts.research_memory import subject_snapshot as _snap

    return _snap(subject) or f"No structured memory for {subject!r}."


@mcp.tool()
def company_summary(ticker: str) -> str:
    """The stored research summary markdown for a covered ticker (e.g.
    '2330 TT', 'MU', '6857 JT'). This is the pipeline's rolling synthesis of
    all sellside research stored for that company."""
    t = ticker.strip()
    md = _read(DATA / t / "research" / "summary.md")
    return md or f"No research summary stored for {t!r} (check ticker format, e.g. '2330 TT', 'MU')."


@mcp.tool()
def theme_summary(theme: str) -> str:
    """The stored summary for a thematic folder (e.g. 'Memory', 'WFE',
    'AI Infrastructure', 'Photonics')."""
    t = theme.strip()
    md = _read(DATA / "Thematic" / t / "theme_summary.md")
    if md:
        return md
    themes = sorted(p.name for p in (DATA / "Thematic").iterdir() if p.is_dir())
    return f"No summary for theme {t!r}. Available themes: {', '.join(themes)}"


@mcp.tool()
def latest_tech_brief() -> str:
    """The most recent Tech Brief (ranked semiconductor/AI supply-chain
    headlines with summaries). Use for 'what happened today/recently'."""
    path = DATA / "_headlines" / "_latest_digest.json"
    if not path.exists():
        return "No tech brief stored yet."
    payload = json.loads(path.read_text(encoding="utf-8"))
    return f"Generated {payload.get('generated_at')}\n\n{payload.get('brief', '')}"


if __name__ == "__main__":
    mcp.run()
