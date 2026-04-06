"""Batch store macro research files — wave 2."""
import sys
import os
import json
import shutil
from pathlib import Path
from datetime import datetime

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from anthropic import Anthropic
from scripts.macro import (
    _ensure_dirs, _extract_file_text, _build_extraction_prompt,
    _parse_json_response, _update_themes, _rebuild_author_summary,
    _rebuild_macro_summary, PROJECT_ROOT, MACRO_DIR,
)

files = [
    (r"C:\Users\Owner\Downloads\Telegram Desktop\IT'S_FINALLY_TIME_TO_LEAN_INTO_CORPORATE_CREDIT_The_MacroTourist.pdf", "Kevin Muir (MacroTourist)"),
    (r"C:\Users\Owner\Downloads\Telegram Desktop\But, what if_ - by Richard Excell - Stay Vigilant.pdf", "Richard Excell (Stay Vigilant)"),
    (r"C:\Users\Owner\Downloads\Telegram Desktop\2_Oil_At_US$250_bbl_by_Michael_Howell_Capital_Wars.pdf", "Michael Howell"),
    (r"C:\Users\Owner\Downloads\Telegram Desktop\CMR_03.15.2026_Final_.pdf", "Unknown"),
    (r"C:\Users\Owner\Downloads\Telegram Desktop\The_Cascade_Vol_388_Ss - Chase Taylor.pdf", "Chase Taylor"),
    (r"C:\Users\Owner\Downloads\Telegram Desktop\The Reaction - by Danny Dayan - Macro Musings by Danny D.pdf", "Danny Dayan"),
    (r"C:\Users\Owner\Downloads\Telegram Desktop\Walk in the Pines #388 - by Chase Taylor.pdf", "Chase Taylor"),
    (r"C:\Users\Owner\Downloads\Telegram Desktop\Attack the Week (ATW) - by Paper Alfa - PA - Global Macro.pdf", "Paper Alfa (PA Global Macro)"),
    (r"C:\Users\Owner\Downloads\Telegram Desktop\Iran is playing a long game.pdf", "Unknown"),
    (r"C:\Users\Owner\Downloads\Telegram Desktop\the halls of montezuma - by Alyosha - market vibes.pdf", "Alyosha (Market Vibes)"),
]


def store_fast(file_path, author, client):
    author_dir = _ensure_dirs(author)
    notes_dir = author_dir / "notes"

    src = Path(file_path)
    if not src.exists():
        print(f"  File not found: {src.name}")
        return False

    dest_name = f"{datetime.now().strftime('%Y-%m-%d')}_{src.name}"
    json_path = notes_dir / f"{dest_name}.json"

    if json_path.exists():
        print(f"  Already stored, skipping.")
        return True

    shutil.copy2(src, notes_dir / dest_name)
    text = _extract_file_text(src)
    print(f"  Extracted {len(text):,} chars")

    prompt = _build_extraction_prompt(author) + text
    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    note_data = _parse_json_response(resp.content[0].text)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(note_data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    meta = note_data.get("metadata", {})
    print(f"  Date:  {meta.get('date', '?')}")
    print(f"  Title: {meta.get('title', '?')}")
    _update_themes(note_data, author)
    return True


def main():
    client = Anthropic(max_retries=3, timeout=180.0)
    total = len(files)
    success = 0
    failed = 0
    authors_touched = set()

    for i, (path, author) in enumerate(files, 1):
        basename = os.path.basename(path)
        print(f"\n[{i}/{total}] {basename}")
        print(f"  Author: {author}")
        try:
            if store_fast(path, author, client):
                success += 1
                authors_touched.add(author)
            else:
                failed += 1
        except Exception as e:
            safe = str(e).encode("ascii", errors="replace").decode()
            print(f"  ERROR: {safe[:120]}")
            failed += 1

    print(f"\n{'='*60}")
    print(f"Extraction complete: {success} stored, {failed} failed")
    print(f"Rebuilding summaries for {len(authors_touched)} authors...")

    for author in sorted(authors_touched):
        author_dir = MACRO_DIR / "authors" / author
        print(f"  {author}...")
        _rebuild_author_summary(author_dir)

    print("Rebuilding macro summary...")
    _rebuild_macro_summary()
    print(f"\n{'='*60}")
    print("BATCH COMPLETE")


if __name__ == "__main__":
    main()
