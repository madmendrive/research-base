"""Batch store macro research files — optimized for bulk imports.
Skips the second-pass analysis and defers summary rebuilds to the end."""
import sys
import os
import json
import shutil
from pathlib import Path
from datetime import datetime

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from anthropic import Anthropic
from scripts.macro import (
    _ensure_dirs, _extract_file_text, _build_extraction_prompt,
    _call_api, _parse_json_response, _rebuild_author_summary,
    _update_themes, _rebuild_macro_summary, PROJECT_ROOT,
)

files = [
    (r"C:\Users\Owner\Downloads\AMFX                   The Reflex                 10MAR26 - Spectra Markets.pdf", "Brent Donnelly (Spectra Markets)"),
    (r"C:\Users\Owner\Downloads\Convexity now goes both ways - Spectra Markets.pdf", "Brent Donnelly (Spectra Markets)"),
    (r"C:\Users\Owner\Downloads\It's All About Terms Of Trade - Spectra Markets.pdf", "Brent Donnelly (Spectra Markets)"),
    (r"C:\Users\Owner\Downloads\The Perilous State of FCI - by Danny Dayan.pdf", "Danny Dayan"),
    (r"C:\Users\Owner\Downloads\Weekly Indicators - by Danny Dayan (1).pdf", "Danny Dayan"),
    (r"C:\Users\Owner\Downloads\Weekly Indicators - by Danny Dayan.pdf", "Danny Dayan"),
    (r"C:\Users\Owner\Downloads\Global Liquidity Watch_ Weekly Update - by Michael Howell (1).pdf", "Michael Howell"),
    (r"C:\Users\Owner\Downloads\Careful What You Wish For - by Michael Howell.pdf", "Michael Howell"),
    (r"C:\Users\Owner\Downloads\Signal & Noise Filter - David Cervantes _ Pinebrook Capital (1).pdf", "David Cervantes (Pinebrook Capital)"),
    (r"C:\Users\Owner\Downloads\U.S. Economic Growth Update - David Cervantes.pdf", "David Cervantes (Pinebrook Capital)"),
    (r"C:\Users\Owner\Downloads\M'tourist Private Feed Recap - by Kevin Muir (5).pdf", "Kevin Muir (MacroTourist)"),
    (r"C:\Users\Owner\Downloads\M'tourist Private Feed Recap - by Kevin Muir (4).pdf", "Kevin Muir (MacroTourist)"),
    (r"C:\Users\Owner\Downloads\(3) GEOPOLITICS DOESN'T CHANGE PRICES IN THE LONG-RUN_ - MacroTourist.pdf", "Kevin Muir (MacroTourist)"),
    (r"C:\Users\Owner\Downloads\REPORTS OF THE DEATH OF THE BUSINESS CYCLE ARE VASTLY EXAGGERATED - The MacroTourist.pdf", "Kevin Muir (MacroTourist)"),
    (r"C:\Users\Owner\Downloads\Scorched earth - by Geo Chen - Fidenza Macro.pdf", "Geo Chen (Fidenza Macro)"),
    (r"C:\Users\Owner\Downloads\Sh_ts getting real - by Geo Chen - Fidenza Macro.pdf", "Geo Chen (Fidenza Macro)"),
    (r"C:\Users\Owner\Downloads\how to open the strait of hormuz - by Alyosha.pdf", "Alyosha (Market Vibes)"),
    (r"C:\Users\Owner\Downloads\perfect is the enemy of good - by Alyosha - market vibes.pdf", "Alyosha (Market Vibes)"),
    (r"C:\Users\Owner\Downloads\friday wrap, march 13 - by Alyosha - market vibes.pdf", "Alyosha (Market Vibes)"),
    (r"C:\Users\Owner\Downloads\the cat bird seat - by Alyosha - market vibes.pdf", "Alyosha (Market Vibes)"),
    (r"C:\Users\Owner\Downloads\The Anatomy of a Crash - PauloMacro's Substack.pdf", "PauloMacro"),
    (r"C:\Users\Owner\Downloads\Make 1973 Great Again - PauloMacro's Substack.pdf", "PauloMacro"),
    (r"C:\Users\Owner\Downloads\Oil, Jobs, Bond Failure, and Other Fun - Paolo Macro.pdf", "PauloMacro"),
    (r"C:\Users\Owner\Downloads\Friday Thoughts - by Paper Alfa - PA - Global Macro.pdf", "Paper Alfa (PA Global Macro)"),
    (r"C:\Users\Owner\Downloads\Watch_Credit_-_by_Alexander_Campbell_-_Campbell_Ramble.pdf", "Alexander Campbell (Campbell Ramble)"),
    (r"C:\Users\Owner\Downloads\It Takes Two to TACO - by Le Shrub.pdf", "Le Shrub"),
    (r"C:\Users\Owner\Downloads\(1) Walk in the Pines #387 - by Chase Taylor.pdf", "Chase Taylor"),
    (r"C:\Users\Owner\Downloads\it_ain_t_over_till_the_fat_derivatives_lady_sings.pdf", "Unknown"),
    (r"C:\Users\Owner\Downloads\US intervention in oil futures would be 'biblical disaster', CME warns.pdf", "News Article"),
]


def store_fast(file_path, author, client):
    """Store a macro note with first-pass extraction only. No second pass, no summary rebuild."""
    author_dir = _ensure_dirs(author)
    notes_dir = author_dir / "notes"

    src = Path(file_path)
    if not src.exists():
        print(f"  File not found: {file_path}")
        return False

    dest_name = f"{datetime.now().strftime('%Y-%m-%d')}_{src.name}"
    dest = notes_dir / dest_name

    # Skip if already stored
    json_path = notes_dir / f"{dest_name}.json"
    if json_path.exists():
        print(f"  Already stored, skipping.")
        return True

    shutil.copy2(src, dest)

    # Extract text
    text = _extract_file_text(src)
    print(f"  Extracted {len(text):,} chars")

    # First pass only — extraction + Key Points + Trader's Brief
    prompt = _build_extraction_prompt(author) + text
    raw = _call_api(client, [{"role": "user", "content": prompt}], max_tokens=8192)
    note_data = _parse_json_response(raw)

    # Save JSON (without second pass — analysis_report has PENDING_SECOND_PASS)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(note_data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    meta = note_data.get("metadata", {})
    print(f"  Date:  {meta.get('date', '?')}")
    print(f"  Title: {meta.get('title', '?')}")

    # Update themes inline (cheap, no API call)
    _update_themes(note_data, author)
    return True


def main():
    client = Anthropic()
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

    # Deferred: rebuild all author summaries + macro summary once at the end
    print(f"\n{'='*60}")
    print(f"Extraction complete: {success} stored, {failed} failed")
    print(f"Rebuilding summaries for {len(authors_touched)} authors...")

    from scripts.macro import MACRO_DIR
    for author in sorted(authors_touched):
        author_dir = MACRO_DIR / "authors" / author
        print(f"  {author}...")
        _rebuild_author_summary(author_dir)

    print("Rebuilding macro summary...")
    _rebuild_macro_summary()

    print(f"\n{'='*60}")
    print(f"BATCH COMPLETE")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
