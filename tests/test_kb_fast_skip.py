"""Tests for the stat-based fast-skip in kb.index_file / reindex_source.

Unchanged files (same mtime + size as recorded at index time) must be skipped
without text extraction; changed files must be re-indexed; legacy rows without
a stat stamp get stamped on the first hash-unchanged pass.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import kb


class TestFastSkip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.conn = kb.connect(root / "kb.sqlite")
        self.addCleanup(self.conn.close)
        self.doc = root / "note.md"
        self.doc.write_text("# Heading\n\nSome research content about HBM supply.",
                            encoding="utf-8")

    def test_unchanged_file_skips_without_extraction(self):
        first = kb.index_file(self.doc, embed=False, conn=self.conn)
        self.assertTrue(first["indexed"])

        with mock.patch.object(kb, "extract_text_from_file",
                               side_effect=AssertionError("must not extract")):
            second = kb.index_file(self.doc, embed=False, conn=self.conn)
        self.assertFalse(second["indexed"])
        self.assertEqual(second["reason"], "unchanged_stat")

    def test_changed_file_is_reindexed(self):
        kb.index_file(self.doc, embed=False, conn=self.conn)
        self.doc.write_text("# Heading\n\nCompletely different content now.",
                            encoding="utf-8")
        result = kb.index_file(self.doc, embed=False, conn=self.conn)
        self.assertTrue(result["indexed"])

    def test_force_bypasses_stat_skip(self):
        kb.index_file(self.doc, embed=False, conn=self.conn)
        result = kb.index_file(self.doc, embed=False, force=True, conn=self.conn)
        self.assertTrue(result["indexed"])

    def test_legacy_row_without_stamp_gets_stamped_then_skips(self):
        kb.index_file(self.doc, embed=False, conn=self.conn)
        # Simulate a pre-fast-skip row: strip the stat stamp.
        self.conn.execute("UPDATE documents SET metadata_json = '{}' WHERE source_path = ?",
                          (str(self.doc),))
        self.conn.commit()

        # Extraction happens (hash check), but the row gets re-stamped...
        second = kb.index_file(self.doc, embed=False, conn=self.conn)
        self.assertFalse(second["indexed"])
        self.assertEqual(second["reason"], "unchanged")

        # ...so the next pass skips without opening the file.
        with mock.patch.object(kb, "extract_text_from_file",
                               side_effect=AssertionError("must not extract")):
            third = kb.index_file(self.doc, embed=False, conn=self.conn)
        self.assertEqual(third["reason"], "unchanged_stat")

    def test_same_size_touch_only_reindexes_on_mtime_change(self):
        kb.index_file(self.doc, embed=False, conn=self.conn)
        os.utime(self.doc, ns=(123456789, 987654321))
        result = kb.index_file(self.doc, embed=False, conn=self.conn)
        # mtime changed, size same -> extraction runs, hash matches -> unchanged,
        # and the new mtime is re-stamped for future skips.
        self.assertEqual(result["reason"], "unchanged")
        with mock.patch.object(kb, "extract_text_from_file",
                               side_effect=AssertionError("must not extract")):
            again = kb.index_file(self.doc, embed=False, conn=self.conn)
        self.assertEqual(again["reason"], "unchanged_stat")


if __name__ == "__main__":
    unittest.main()
