"""One-shot: remove image-only BofA note copies superseded by a text-layer
copy of the same note. Groups stored BofA PDFs by normalized title; for any
group with both an image-only and a text-layer member, quarantines the
image-only one (file + sidecars) and prunes its KB + research-memory rows,
reusing the dedup_notes machinery. Text-layer copies always win.
"""
import glob
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import truststore
truststore.inject_into_ssl()

from scripts import kb
from scripts.classifier import extract_text
from scripts import dedup_notes

DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}_")


def norm_title(p: Path) -> str:
    name = DATE_PREFIX.sub("", p.name).rsplit(".pdf", 1)[0]
    name = re.sub(r"(?i)^bofa[_ ]+(securities[_ ]+)?", "", name)
    name = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    return name


def has_text_layer(p: Path) -> bool:
    try:
        text, err = extract_text(p, max_pages=5, max_chars=4000)
    except Exception:
        return False
    return bool(text and len(text.strip()) > 200)


def main(apply: bool):
    data = kb.DATA_DIR
    hits = set(glob.glob(str(data / "**" / "notes" / "*[Bb]ofa*.pdf"), recursive=True))
    hits |= set(glob.glob(str(data / "**" / "notes" / "*BofA*.pdf"), recursive=True))
    groups: dict[str, list[Path]] = defaultdict(list)
    for h in hits:
        groups[norm_title(Path(h))].append(Path(h))

    removals = []
    for title, paths in groups.items():
        if len(paths) < 2:
            continue
        text_layer = [p for p in paths if has_text_layer(p)]
        image_only = [p for p in paths if not has_text_layer(p)]
        if text_layer and image_only:
            for p in image_only:
                removals.append((title, p, text_layer[0]))

    print(f"BofA note titles: {len(groups)}; collisions to fix: {len(removals)}")
    for title, victim, keeper in removals:
        print(f"  REMOVE image-only: {victim.relative_to(data)}")
        print(f"    keep text-layer: {keeper.relative_to(data)}")

    if not apply or not removals:
        print("(dry-run)" if not apply else "(nothing to remove)")
        return

    conn = kb.connect()
    quarantine = data / "_dedup_quarantine" / "bofa_imageonly"
    quarantine.mkdir(parents=True, exist_ok=True)
    import shutil
    for _title, victim, _keeper in removals:
        rel = victim.relative_to(data).as_posix()
        source_uri = f"research-structured:{rel}.json"
        dedup_notes._delete_db_rows(conn, victim, source_uri)
        for f in dedup_notes._sidecar_files(victim):
            frel = f.relative_to(data).as_posix().replace("/", "__")
            shutil.move(str(f), str(quarantine / frel))
        conn.commit()
        print(f"  removed {rel}")
    conn.close()
    print(f"done; quarantined to {quarantine}")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
