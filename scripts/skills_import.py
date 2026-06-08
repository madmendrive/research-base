"""Import existing Claude/Codex skill files into the KB as searchable knowledge."""

from __future__ import annotations

import shutil
from pathlib import Path

from scripts import kb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SKILLS_DIR = DATA_DIR / "_skills"
SUPPORTED = {".md", ".txt", ".json", ".yaml", ".yml"}


def import_skills(source_dir: str | Path, dry_run: bool = False, force: bool = False) -> dict:
    src = Path(source_dir).expanduser().resolve()
    if not src.exists() or not src.is_dir():
        raise FileNotFoundError(src)

    files = [p for p in src.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED]
    stats = {"source": str(src), "files": len(files), "copied": 0, "indexed": 0, "dry_run": dry_run}
    if dry_run:
        return stats

    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    conn = kb.connect()
    try:
        for path in files:
            try:
                rel = path.relative_to(src)
            except ValueError:
                rel = Path(path.name)
            dest = SKILLS_DIR / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if force or not dest.exists() or kb.file_hash(path) != kb.file_hash(dest):
                shutil.copy2(path, dest)
                stats["copied"] += 1
            result = kb.index_file(
                dest,
                source_type="skills",
                metadata={"source_dir": str(src), "source_relative_path": rel.as_posix()},
                force=force,
                conn=conn,
            )
            if result.get("indexed"):
                stats["indexed"] += 1
    finally:
        conn.close()
    return stats
