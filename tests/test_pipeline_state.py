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


class SchemaEnrichmentTests(unittest.TestCase):
    def test_dynamic_forecast_years_in_extraction_prompt(self):
        from datetime import datetime

        from scripts.research import _build_extraction_prompt

        prompt = _build_extraction_prompt("Apple", "AAPL")
        year = datetime.now().year
        self.assertIn(f"FY{year}", prompt)
        self.assertIn(f"FY{year + 2}", prompt)
        self.assertNotIn("FY2025\"", prompt if year != 2025 else "")
        for field in ("segment_estimates", "valuation", "industry_assumptions", "primer_concepts"):
            self.assertIn(field, prompt)

    def test_enrichment_fields_reach_structured_memory(self):
        import sqlite3

        from scripts import kb
        from scripts.research_memory import _ingest_enrichment, init_schema

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_schema(conn)
        scope = {"corpus_type": "single_name", "subject_type": "ticker", "subject": "AAPL"}
        meta = {"author": "Analyst", "publisher": "Firm", "published_at": "2026-06-01"}
        payload = {
            "segment_estimates": [
                {"segment": "Services", "metric": "revenue", "values": {"FY2026": 120}, "unit": "USD billions"}
            ],
            "valuation": {
                "methodology": "25x FY2027 EPS",
                "base_case_target": 280,
                "key_assumptions": ["Services grows 15%"],
            },
            "industry_assumptions": [
                {"metric": "CY2026 WFE", "value": "$135bn", "period": "CY2026", "basis": "bottom-up fab tracker"}
            ],
            "primer_concepts": [
                {"concept": "CoWoS-L", "explanation": "Large-interposer packaging.", "why_it_matters": "AI GPU capacity gate."}
            ],
        }
        _ingest_enrichment(conn, "test:src", scope, meta, payload)
        estimates = {r["metric"] for r in conn.execute("SELECT metric FROM research_estimates")}
        self.assertIn("Services.revenue", estimates)
        self.assertIn("valuation.methodology", estimates)
        self.assertIn("valuation.base_case_target", estimates)
        self.assertIn("CY2026 WFE", estimates)
        views = {(r["theme"], r["category"]) for r in conn.execute("SELECT theme, category FROM research_views")}
        self.assertIn(("CoWoS-L", "primer_concept"), views)
        self.assertIn(("valuation assumptions", "valuation"), views)


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
