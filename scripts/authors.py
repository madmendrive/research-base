"""Author-name canonicalization.

Macro/Semis research is stored under a folder named after the author string
triage extracts. Triage often spells the same writer differently across docs
(e.g. "Chase Taylor (Pinecone Macro)" vs "Chase Taylor (Walk in the Pines)"),
which would split one author's history across multiple folders.

`config/author_aliases.json` maps variant spellings -> a canonical name. This
module resolves a raw author string to its canonical form at the storage
choke point (scripts.macro.store_macro) and at folder auto-creation
(scripts.bulk_ingest._auto_add_macro_authors), so both macro and semis paths
collapse to a single folder. Mirrors the ticker alias_map() in scripts.tickers.

The map is read fresh on each call (it's tiny) so edits take effect in the
long-running worker processes without a restart.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALIASES_PATH = PROJECT_ROOT / "config" / "author_aliases.json"


def _norm(name: str | None) -> str:
    """Normalize a name for alias lookup: trim, collapse whitespace, casefold."""
    return re.sub(r"\s+", " ", (name or "").strip()).casefold()


def _alias_map() -> dict[str, str]:
    """variant-key (normalized) -> canonical author name. Empty on any error."""
    try:
        with open(ALIASES_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {_norm(k): str(v) for k, v in raw.items() if v}


def canonicalize_author(name: str | None) -> str | None:
    """Return the canonical author name for `name`, or `name` unchanged."""
    if not name:
        return name
    return _alias_map().get(_norm(name), name)
