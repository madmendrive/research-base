"""Shared file-write helpers.

Summary/state JSON files are read concurrently by the bot, sweeper, and
heartbeat, so writers must go through an atomic tmp+rename to avoid a reader
catching a half-written file.
"""

from __future__ import annotations

import json
from pathlib import Path


def write_json_atomic(path: Path, data, *, indent: int = 2, trailing_newline: bool = True) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=indent, ensure_ascii=False)
    if trailing_newline:
        text += "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
