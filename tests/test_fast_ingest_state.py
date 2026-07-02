"""Fast-ingest state file: atomic writes and corruption recovery."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import parallel_ingest as P


class TestFastIngestState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.object(P, "STATE_PATH", Path(self._tmp.name) / "_fast_ingest_state.json")
        p.start()
        self.addCleanup(p.stop)

    def test_save_then_load_roundtrip(self):
        P._save_state({"committed": {"abc": {}}, "failed": {}, "runs": []})
        self.assertEqual(list(P._load_state()["committed"]), ["abc"])
        # Atomic write leaves no temp file behind.
        self.assertFalse(P.STATE_PATH.with_suffix(".tmp").exists())

    def test_corrupt_state_recovers_instead_of_crashing(self):
        # Regression: a torn write used to crash every subsequent ingest_file
        # job at json.loads until the file was repaired by hand.
        P.STATE_PATH.write_text('{"committed": {"abc"', encoding="utf-8")
        with self.assertLogs("parallel_ingest", level="ERROR"):
            state = P._load_state()
        self.assertEqual(state, {"committed": {}, "failed": {}, "runs": []})
        # The corrupt file is preserved for postmortem.
        self.assertTrue(P.STATE_PATH.with_suffix(".corrupt").exists())

    def test_missing_state_returns_empty(self):
        self.assertEqual(P._load_state()["committed"], {})


if __name__ == "__main__":
    unittest.main()
