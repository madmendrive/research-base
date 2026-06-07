"""Semiconductors research storage — thin wrapper over scripts/macro.py.

Stores publisher/author-driven semis research (SemiAnalysis, Damnang, PhotonCap,
Vikram Sekar, etc.) under data/Semis/ with the same author-tracking + view-evolution
logic that scripts/macro.py provides for true-macro authors.

Implementation: wraps each call in macro.use_category("Semis"), which redirects
storage via a ContextVar. Thread-safe and asyncio-safe — concurrent bot uploads
and sweeper jobs never collide.
"""

from pathlib import Path

from scripts import macro as _macro

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SEMIS_DIR = PROJECT_ROOT / "data" / "Semis"


def store_semis(file_path, author):
    with _macro.use_category("Semis"):
        _macro.store_macro(file_path, author)


def analyse_semis(file_path):
    with _macro.use_category("Semis"):
        _macro.analyse_macro(file_path)


def show_semis_summary():
    with _macro.use_category("Semis"):
        _macro.show_macro_summary()


def show_author_summary(author):
    with _macro.use_category("Semis"):
        _macro.show_author_summary(author)


def show_author_history(author, topic=None):
    with _macro.use_category("Semis"):
        _macro.show_author_history(author, topic)


def list_semis_authors():
    with _macro.use_category("Semis"):
        _macro.list_macro_authors()
