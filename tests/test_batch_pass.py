"""Batches-API view-evolution backfill: scanning, submit state, apply."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import batch_pass as B
from scripts import bulk_cross_cut as CC


def _write_note(base: Path, rel: str, report="intro\n\nPENDING_SECOND_PASS"):
    path = base / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "metadata": {"title": "T", "source": "Firm", "date": "2026-07-01"},
        "analysis_report": report,
    }), encoding="utf-8")
    return path


class TestScanAndSubmit(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name) / "data"
        for name, value in {
            "DATA_DIR": self.data,
            "STATE_PATH": self.data / "_batch_second_pass_state.json",
        }.items():
            p = mock.patch.object(B, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_scan_skips_done_news_and_prefilter(self):
        _write_note(self.data, "FAKE/research/notes/2026-07-01_a.pdf.json")
        _write_note(self.data, "FAKE/research/notes/2026-06-01_old.pdf.json")
        _write_note(self.data, "Macro/authors/News Article/notes/2026-07-01_n.pdf.json")
        _write_note(self.data, "Semis/authors/A1/notes/2026-07-02_b.pdf.json",
                    report="done")  # non-empty, no placeholder -> done
        pending = B.scan_pending_notes(since="2026-07-01")
        self.assertEqual(
            [Path(p["json_path"]).name for p in pending], ["2026-07-01_a.pdf.json"])
        self.assertEqual(pending[0]["primary_type"], "single_name")
        self.assertEqual(pending[0]["subject"], "FAKE")
        # No since filter picks up June too.
        self.assertEqual(len(B.scan_pending_notes()), 2)

    def test_submit_records_state_and_dry_run_costs_nothing(self):
        _write_note(self.data, "FAKE/research/notes/2026-07-01_a.pdf.json")
        dry = B.submit_batch(dry_run=True)
        self.assertEqual(dry["requests"], 1)
        self.assertGreater(dry["estimated_cost_usd"], 0)
        self.assertFalse(B.STATE_PATH.exists())

        fake_batch = SimpleNamespace(id="msgbatch_1", processing_status="in_progress")
        client = mock.Mock()
        client.messages.batches.create.return_value = fake_batch
        with mock.patch.object(B, "_client", return_value=client):
            stats = B.submit_batch()
        self.assertEqual(stats["batch_id"], "msgbatch_1")
        (req,) = client.messages.batches.create.call_args.kwargs["requests"]
        self.assertEqual(req["params"]["max_tokens"], 16384)
        self.assertIn("NEW RESEARCH NOTE", req["params"]["messages"][0]["content"])
        state = json.loads(B.STATE_PATH.read_text(encoding="utf-8"))
        (items,) = [state["batches"]["msgbatch_1"]["items"]]
        (item,) = items.values()
        self.assertTrue(item["json_path"].endswith("2026-07-01_a.pdf.json"))


class TestApplyBatch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name) / "data"
        p = mock.patch.object(B, "STATE_PATH", self.data / "_batch_state.json")
        p.start()
        self.addCleanup(p.stop)
        self.note = _write_note(self.data, "FAKE/research/notes/2026-07-01_a.pdf.json")
        self.cid = B._custom_id(str(self.note))
        self.data.mkdir(parents=True, exist_ok=True)
        B._save_state({"batches": {"msgbatch_1": {
            "submitted_at": "2026-07-13T00:00:00+00:00",
            "items": {self.cid: {
                "json_path": str(self.note), "primary_type": "single_name",
                "subject": "FAKE", "category": "Macro",
            }},
        }}})

    def _client_with(self, status="ended", results=()):
        client = mock.Mock()
        client.messages.batches.retrieve.return_value = SimpleNamespace(
            processing_status=status, request_counts=SimpleNamespace(
                succeeded=1, errored=0, processing=0, canceled=0, expired=0))
        client.messages.batches.results.return_value = iter(results)
        return client

    def test_not_ready_before_ended(self):
        with mock.patch.object(B, "_client", return_value=self._client_with("in_progress")):
            out = B.apply_batch("msgbatch_1")
        self.assertEqual(out["status"], "not_ready")

    def test_apply_merges_and_rebuilds_once(self):
        result = SimpleNamespace(
            custom_id=self.cid,
            result=SimpleNamespace(type="succeeded", message=SimpleNamespace(
                content=[SimpleNamespace(type="text", text="THE EVOLUTION")])))
        with mock.patch.object(B, "_client", return_value=self._client_with(results=[result])), \
                mock.patch("scripts.research.rebuild_summary") as rebuild:
            out = B.apply_batch("msgbatch_1")
        self.assertEqual(out["applied"], 1)
        self.assertEqual(out["entities_rebuilt"], 1)
        rebuild.assert_called_once_with("FAKE")
        saved = json.loads(self.note.read_text(encoding="utf-8"))
        self.assertIn("THE EVOLUTION", saved["analysis_report"])
        self.assertTrue(saved["metadata"]["second_pass_done"])
        md = self.note.with_name("2026-07-01_a.pdf_summary.md")
        self.assertIn("THE EVOLUTION", md.read_text(encoding="utf-8"))
        # Re-apply is a no-op (note stamped) — no double merge, no rebuild.
        with mock.patch.object(B, "_client", return_value=self._client_with(results=[result])), \
                mock.patch("scripts.research.rebuild_summary") as rebuild2:
            again = B.apply_batch("msgbatch_1")
        self.assertEqual(again["applied"], 0)
        self.assertEqual(again["already_done"], 1)
        rebuild2.assert_not_called()


class TestFastIngestCorpus(unittest.TestCase):
    def test_fast_records_are_materiality_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            staging = data / "_staging_ingest"
            staging.mkdir()
            digest = "a" * 64
            (staging / f"{digest}.json").write_text(json.dumps({
                "source_path": str(data / "inbox" / "x.pdf"),
                "triage": {
                    "primary_type": "single_name", "primary_subject": "FAKE",
                    "tickers_covered": ["FAKE", "OTHER", "NOISE", "OTHER"],
                    "themes_touched": ["Memory", "Memory"],
                    "materiality": {"tickers": {"NOISE": "passing"}, "themes": {}},
                },
            }), encoding="utf-8")
            state = data / "_fast_ingest_state.json"
            state.write_text(json.dumps({"committed": {
                digest: {"file": "x.pdf", "stored_path": str(data / "stored.pdf"),
                         "status": "research"},
                "b" * 64: {"file": "held.pdf", "status": "pending_review"},
            }}), encoding="utf-8")
            known = {"FAKE", "OTHER", "NOISE"}
            with mock.patch.object(CC, "FAST_STATE_PATH", state), \
                    mock.patch.object(CC, "FAST_STAGING_DIR", staging), \
                    mock.patch("scripts.triage._load_companies",
                               return_value={t: {} for t in known}), \
                    mock.patch("scripts.triage._existing_themes",
                               return_value=["Memory"]):
                records = CC._fast_ingest_records()
        self.assertEqual(list(records), [digest])
        rec = records[digest]
        # NOISE dropped by materiality; unknown tickers/themes would be
        # dropped by the validity gate (all of these are "known" here).
        self.assertEqual(rec["tickers_covered"], ["FAKE", "OTHER"])
        self.assertEqual(rec["themes_touched"], ["Memory"])
        self.assertEqual(rec["source_path"], str(data / "stored.pdf"))


if __name__ == "__main__":
    unittest.main()
