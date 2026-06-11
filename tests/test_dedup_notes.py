"""Tests for scripts.dedup_notes — duplicate detection, safe apply, prevention.

Uses a temp data tree + temp sqlite KB; research_memory.DATA_DIR is patched so
source URIs resolve against the temp tree. The real config/companies.json is
read for canonical-ticker checks (MU, KLAC, 000660 KS are long-standing
entries).
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import kb, research_memory
from scripts import dedup_notes


def _payload(n_estimates: int, title: str = "Note") -> dict:
    return {
        "metadata": {"title": title, "author": "JPM", "source": "JPM",
                     "date": "2026-06-01", "source_type": "sellside_research"},
        "key_estimates": {"revenue": {f"FY2{i}": f"{100 + i}" for i in range(n_estimates)}},
        "key_themes": [{"theme": "HBM", "view": "supply stays tight", "sentiment": "positive"}],
    }


class DedupNotesBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.data_dir = root / "data"
        self.data_dir.mkdir()
        self.conn = kb.connect(root / "kb.sqlite")
        self.addCleanup(self.conn.close)
        patcher = mock.patch.object(research_memory, "DATA_DIR", self.data_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_note(self, rel_dir: str, name: str, content: bytes,
                  payload: dict | None = None) -> Path:
        notes_dir = self.data_dir / rel_dir
        notes_dir.mkdir(parents=True, exist_ok=True)
        pdf = notes_dir / name
        pdf.write_bytes(content)
        if payload is not None:
            json_path = pdf.with_name(pdf.name + ".json")
            json_path.write_text(json.dumps(payload), encoding="utf-8")
            research_memory.ingest_file(self.conn, json_path)
            self.conn.commit()
        return pdf

    def rows_for(self, pdf: Path) -> int:
        rel = pdf.relative_to(self.data_dir).as_posix()
        uri = f"research-structured:{rel}.json"
        total = self.conn.execute(
            "SELECT COUNT(*) FROM research_sources WHERE source_uri = ?", (uri,)
        ).fetchone()[0]
        for table in dedup_notes.RESEARCH_CHILD_TABLES:
            total += self.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE source_uri = ?", (uri,)
            ).fetchone()[0]
        return total


class TestScanAndApply(DedupNotesBase):
    def test_same_dir_duplicate_keeps_richer_earlier_copy(self):
        content = b"%PDF-fake-bytes%" * 64
        rich = self.make_note("MU/research/notes", "2026-06-08_note.pdf",
                              content, _payload(3))
        sparse = self.make_note("MU/research/notes", "2026-06-10_note.pdf",
                                content, _payload(1))
        rich_rows = self.rows_for(rich)
        self.assertGreater(rich_rows, self.rows_for(sparse))

        plan = dedup_notes.scan_duplicates(self.data_dir, self.conn)
        self.assertEqual(plan["dedupe_groups"], 1)
        group = plan["groups"][0]
        self.assertEqual(group["keeper"], str(rich))
        self.assertEqual(group["losers"], [str(sparse)])
        self.assertIsNone(group["donor"])

        result = dedup_notes.apply_plan(plan, conn=self.conn, data_dir=self.data_dir)
        self.assertEqual(result["groups_applied"], 1)
        self.assertEqual(result["warnings"], [])
        self.assertFalse(sparse.exists())
        self.assertFalse(sparse.with_name(sparse.name + ".json").exists())
        self.assertTrue(rich.exists())
        self.assertEqual(self.rows_for(sparse), 0)
        self.assertEqual(self.rows_for(rich), rich_rows)
        quarantined = list((self.data_dir / dedup_notes.QUARANTINE_DIRNAME).rglob("*.pdf"))
        self.assertEqual(len(quarantined), 1)

    def test_legacy_dir_loses_but_donates_richer_extraction(self):
        content = b"%PDF-other-bytes%" * 64
        legacy = self.make_note("000660.KS/research/notes", "2026-06-08_note.pdf",
                                content, _payload(4))
        canonical = self.make_note("000660 KS/research/notes", "2026-06-10_note.pdf",
                                   content, _payload(1))
        legacy_rows = self.rows_for(legacy)

        plan = dedup_notes.scan_duplicates(self.data_dir, self.conn)
        self.assertEqual(plan["dedupe_groups"], 1)
        group = plan["groups"][0]
        self.assertEqual(group["keeper"], str(canonical))
        self.assertEqual(group["donor"], str(legacy))

        result = dedup_notes.apply_plan(plan, conn=self.conn, data_dir=self.data_dir)
        self.assertEqual(result["groups_applied"], 1)
        self.assertEqual(result["transplants"], 1)
        self.assertEqual(result["warnings"], [])
        self.assertFalse(legacy.exists())
        self.assertEqual(self.rows_for(legacy), 0)
        # The keeper inherited the richer extraction (donor row count).
        self.assertEqual(self.rows_for(canonical), legacy_rows)
        kept_payload = json.loads(
            canonical.with_name(canonical.name + ".json").read_text(encoding="utf-8"))
        self.assertEqual(len(kept_payload["key_estimates"]["revenue"]), 4)

    def test_two_real_subjects_are_never_deduped(self):
        content = b"%PDF-shared-bytes%" * 64
        mu = self.make_note("MU/research/notes", "2026-06-08_dual.pdf",
                            content, _payload(2))
        klac = self.make_note("KLAC/research/notes", "2026-06-09_dual.pdf",
                              content, _payload(2))

        plan = dedup_notes.scan_duplicates(self.data_dir, self.conn)
        self.assertEqual(plan["dedupe_groups"], 0)
        self.assertEqual(plan["skipped_multi_subject_groups"], 1)

        result = dedup_notes.apply_plan(plan, conn=self.conn, data_dir=self.data_dir)
        self.assertEqual(result["groups_applied"], 0)
        self.assertTrue(mu.exists())
        self.assertTrue(klac.exists())
        self.assertGreater(self.rows_for(mu), 0)
        self.assertGreater(self.rows_for(klac), 0)

    def test_news_article_copy_does_not_block_dedup(self):
        content = b"%PDF-news-bytes%" * 64
        ticker_copy = self.make_note("MU/research/notes", "2026-06-08_n.pdf",
                                     content, _payload(2))
        junk_copy = self.make_note("Macro/authors/News Article/notes",
                                   "2026-06-10_n.pdf", content, _payload(2))

        plan = dedup_notes.scan_duplicates(self.data_dir, self.conn)
        self.assertEqual(plan["dedupe_groups"], 1)
        group = plan["groups"][0]
        self.assertEqual(group["keeper"], str(ticker_copy))
        self.assertIn(str(junk_copy), group["losers"])

        result = dedup_notes.apply_plan(plan, conn=self.conn, data_dir=self.data_dir)
        self.assertEqual(result["groups_applied"], 1)
        self.assertFalse(junk_copy.exists())
        self.assertTrue(ticker_copy.exists())
        self.assertGreater(self.rows_for(ticker_copy), 0)

    def test_different_bytes_never_grouped(self):
        self.make_note("MU/research/notes", "2026-06-08_v.pdf", b"AAAA" * 200)
        self.make_note("MU/research/notes", "2026-06-10_v.pdf", b"BBBB" * 200)
        plan = dedup_notes.scan_duplicates(self.data_dir, self.conn)
        self.assertEqual(plan["duplicate_groups"], 0)

    def test_apply_reverifies_bytes_before_quarantine(self):
        content = b"%PDF-mutate-bytes%" * 64
        self.make_note("MU/research/notes", "2026-06-08_m.pdf", content)
        loser = self.make_note("MU/research/notes", "2026-06-10_m.pdf", content)
        plan = dedup_notes.scan_duplicates(self.data_dir, self.conn)
        self.assertEqual(plan["dedupe_groups"], 1)
        loser.write_bytes(b"CHANGED-AFTER-SCAN" * 64)

        result = dedup_notes.apply_plan(plan, conn=self.conn, data_dir=self.data_dir)
        self.assertEqual(result["files_quarantined"], 0)
        self.assertTrue(loser.exists())
        self.assertTrue(any("content changed" in w for w in result["warnings"]))


class TestPrevention(DedupNotesBase):
    def test_existing_identical_copy_found_under_date_prefix(self):
        content = b"%PDF-prev-bytes%" * 64
        stored = self.make_note("MU/research/notes", "2026-06-08_[Map] x.pdf", content)
        inbox = Path(self._tmp.name) / "[Map] x.pdf"
        inbox.write_bytes(content)
        found = dedup_notes.existing_identical_copy(stored.parent, inbox)
        self.assertEqual(found, stored)

    def test_existing_identical_copy_rejects_different_bytes(self):
        stored = self.make_note("MU/research/notes", "2026-06-08_y.pdf", b"OLD" * 300)
        inbox = Path(self._tmp.name) / "y.pdf"
        inbox.write_bytes(b"NEW" * 300)
        self.assertIsNone(dedup_notes.existing_identical_copy(stored.parent, inbox))

    def test_existing_identical_copy_requires_exact_name_suffix(self):
        content = b"%PDF-suffix-bytes%" * 64
        self.make_note("MU/research/notes", "2026-06-08_prefix z.pdf", content)
        inbox = Path(self._tmp.name) / "z.pdf"
        inbox.write_bytes(content)
        found = dedup_notes.existing_identical_copy(
            self.data_dir / "MU/research/notes", inbox)
        self.assertIsNone(found)


if __name__ == "__main__":
    unittest.main()
