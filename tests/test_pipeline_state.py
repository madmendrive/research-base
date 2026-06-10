"""Tests for cross-process state sharing between the sweeper and bulk-ingest."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import bulk_ingest, sweeper
from scripts.fileio import load_json_cached


def _write_state(path: Path, hashes: dict) -> None:
    path.write_text(json.dumps({"processed": hashes}), encoding="utf-8")


class LoadJsonCachedTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "state.json"

    def test_missing_file_returns_empty(self):
        self.assertEqual(load_json_cached(self.path), {})

    def test_invalid_json_returns_empty(self):
        self.path.write_text("{broken", encoding="utf-8")
        self.assertEqual(load_json_cached(self.path), {})

    def test_reload_on_mtime_change(self):
        _write_state(self.path, {"a": {}})
        self.assertEqual(set(load_json_cached(self.path)["processed"]), {"a"})
        _write_state(self.path, {"a": {}, "b": {}})
        # Force a distinct mtime in case both writes land in the same tick.
        os.utime(self.path, (1000, 2000))
        self.assertEqual(set(load_json_cached(self.path)["processed"]), {"a", "b"})

    def test_unchanged_file_served_from_cache(self):
        _write_state(self.path, {"a": {}})
        first = load_json_cached(self.path)
        self.assertIs(load_json_cached(self.path), first)


class SweeperSeesLiveBulkStateTests(unittest.TestCase):
    def test_bulk_hashes_reflect_updates_after_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            bulk_path = Path(tmp) / "_bulk_ingest_state.json"
            with mock.patch.object(sweeper, "BULK_STATE_PATH", bulk_path):
                self.assertEqual(sweeper._bulk_hashes(), set())
                _write_state(bulk_path, {"abc123": {"file": "x.pdf"}})
                self.assertEqual(sweeper._bulk_hashes(), {"abc123"})
                _write_state(bulk_path, {"abc123": {}, "def456": {}})
                os.utime(bulk_path, (1000, 2000))
                self.assertEqual(sweeper._bulk_hashes(), {"abc123", "def456"})


class BulkIngestSeesSweeperStateTests(unittest.TestCase):
    def test_sweeper_hashes_consulted(self):
        with tempfile.TemporaryDirectory() as tmp:
            sweeper_path = Path(tmp) / "_sweeper_state.json"
            with mock.patch.object(bulk_ingest, "SWEEPER_STATE_PATH", sweeper_path):
                self.assertEqual(bulk_ingest._sweeper_hashes(), set())
                _write_state(sweeper_path, {"feed42": {"file": "y.pdf"}})
                self.assertEqual(bulk_ingest._sweeper_hashes(), {"feed42"})


if __name__ == "__main__":
    unittest.main()
