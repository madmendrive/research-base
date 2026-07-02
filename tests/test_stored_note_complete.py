"""Half-stored notes (PENDING_SECOND_PASS) must be repairable by re-dropping."""

import json
import tempfile
import unittest
from pathlib import Path

from scripts.analysis_report import stored_note_complete


class TestStoredNoteComplete(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _write(self, payload) -> Path:
        p = self.dir / "note.pdf.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        return p

    def test_complete_note_is_complete(self):
        p = self._write({"analysis_report": "## View Evolution\n\nReal content."})
        self.assertTrue(stored_note_complete(p))

    def test_pending_placeholder_is_incomplete(self):
        # Regression: the dedup skip treated any existing JSON as done, so a
        # crash between the extraction save and the Opus second pass left the
        # note permanently half-stored (the 2 stuck macro notes).
        p = self._write({"analysis_report": "Intro\n\nPENDING_SECOND_PASS"})
        self.assertFalse(stored_note_complete(p))

    def test_corrupt_json_is_incomplete(self):
        p = self.dir / "note.pdf.json"
        p.write_text("{truncated", encoding="utf-8")
        self.assertFalse(stored_note_complete(p))

    def test_missing_file_is_incomplete(self):
        self.assertFalse(stored_note_complete(self.dir / "nope.json"))

    def test_missing_report_field_counts_as_complete(self):
        # Old fast-pipeline notes have analysis_report == "" — they finished
        # their (single-pass) flow and must not be re-processed.
        p = self._write({"analysis_report": "", "metadata": {}})
        self.assertTrue(stored_note_complete(p))


if __name__ == "__main__":
    unittest.main()
