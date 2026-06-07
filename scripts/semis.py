"""Semiconductors research storage — thin wrapper over scripts/macro.py.

Stores publisher/author-driven semis research (SemiAnalysis, Damnang, PhotonCap,
Vikram Sekar, etc.) under data/Semis/ with the same author-tracking + view-evolution
logic that scripts/macro.py provides for true-macro authors.

Implementation: temporarily rebinds macro.MACRO_DIR to data/Semis/ for the duration
of each call. Macro and Semis stay completely separate on disk.
"""

import contextlib
from pathlib import Path

from scripts import macro as _macro

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SEMIS_DIR = PROJECT_ROOT / "data" / "Semis"


@contextlib.contextmanager
def _semis_scope():
    """Temporarily point macro module functions at data/Semis/."""
    original = _macro.MACRO_DIR
    _macro.MACRO_DIR = SEMIS_DIR
    try:
        SEMIS_DIR.mkdir(parents=True, exist_ok=True)
        (SEMIS_DIR / "authors").mkdir(parents=True, exist_ok=True)
        yield
    finally:
        _macro.MACRO_DIR = original


def store_semis(file_path, author):
    with _semis_scope():
        _macro.store_macro(file_path, author)


def analyse_semis(file_path):
    with _semis_scope():
        _macro.analyse_macro(file_path)


def show_semis_summary():
    with _semis_scope():
        _macro.show_macro_summary()


def show_author_summary(author):
    with _semis_scope():
        _macro.show_author_summary(author)


def show_author_history(author, topic=None):
    with _semis_scope():
        _macro.show_author_history(author, topic)


def list_semis_authors():
    with _semis_scope():
        _macro.list_macro_authors()
