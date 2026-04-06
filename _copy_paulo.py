import shutil
from pathlib import Path

downloads = Path(r"C:\Users\Owner\Downloads")
for f in downloads.iterdir():
    if "Anatomy" in f.name and "Revisited" in f.name and f.name.endswith(".pdf") and "(1)" not in f.name and "(2)" not in f.name:
        dst = downloads / "PauloMacro_Crash_Revisited.pdf"
        shutil.copy(f, dst)
        print(f"Copied {f.name} -> {dst.name}")
        break
