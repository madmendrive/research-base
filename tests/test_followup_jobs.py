"""Phase 2 of pipeline unification: queued view_evolution / cross_cut jobs."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import parallel_ingest as P
from scripts import second_pass as SP


class TestEnqueueFollowups(unittest.TestCase):
    def _followups(self, triage, digest="d" * 64):
        dest = Path("data/FAKE/research/notes/2026-07-13_note.pdf")
        json_path = dest.with_name(dest.name + ".json")
        with mock.patch("scripts.jobs.enqueue_job", return_value=1) as enq:
            queued = P._enqueue_followups(triage, digest, dest, json_path)
        return queued, enq

    def test_single_name_gets_view_evolution_and_gated_cross_cuts(self):
        triage = {
            "primary_type": "single_name", "primary_subject": "FAKE",
            "tickers_covered": ["FAKE", "OTHER", "NOISE"],
            "themes_touched": ["Memory"],
            "materiality": {"tickers": {"NOISE": "passing"}, "themes": {}},
        }
        queued, enq = self._followups(triage)
        self.assertIn("view_evolution", queued)
        self.assertIn("cross_cut:ticker:OTHER", queued)
        # Primary itself and passing mentions get no cross-cut.
        self.assertNotIn("cross_cut:ticker:FAKE", queued)
        self.assertNotIn("cross_cut:ticker:NOISE", queued)
        self.assertIn("cross_cut:theme:Memory", queued)
        # Dedupe keys pin each pass to the document digest.
        keys = [c.kwargs["dedupe_key"] for c in enq.call_args_list]
        self.assertIn(f"view_evolution:{'d' * 64}", keys)
        self.assertIn(f"cross_cut:{'d' * 64}:ticker:OTHER", keys)

    def test_news_article_gets_cross_cuts_but_no_view_evolution(self):
        triage = {
            "primary_type": "news_article", "primary_subject": "News Article",
            "tickers_covered": ["FAKE"], "themes_touched": [],
            "materiality": {"tickers": {}, "themes": {}},
        }
        queued, _ = self._followups(triage)
        self.assertNotIn("view_evolution", queued)
        self.assertEqual(queued, ["cross_cut:ticker:FAKE"])


class TestRunSecondPass(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.entity_dir = Path(self._tmp.name) / "FAKE" / "research"
        notes = self.entity_dir / "notes"
        notes.mkdir(parents=True)
        self.json_path = notes / "2026-07-13_note.pdf.json"

    def _write_note(self, report=""):
        self.json_path.write_text(json.dumps({
            "metadata": {"title": "T", "source": "Firm", "date": "2026-07-13"},
            "analysis_report": report,
        }), encoding="utf-8")

    def test_already_done_note_is_skipped_without_api_call(self):
        self._write_note(f"## {SP.VIEW_EVOLUTION_MARKER}\n\ndone earlier")
        with mock.patch("scripts.research._call_api") as call:
            result = SP.run_second_pass(
                self.json_path, primary_type="single_name", subject="FAKE")
        self.assertEqual(result["status"], "already_done")
        call.assert_not_called()

    def test_second_pass_merges_report_and_rebuilds(self):
        self._write_note("intro\n\nPENDING_SECOND_PASS")
        (self.entity_dir / "summary.json").write_text(json.dumps({
            "consensus_estimates": {"rev": 1}, "ratings": [],
        }), encoding="utf-8")
        with mock.patch("scripts.research.Anthropic"), \
                mock.patch("scripts.research._call_api", return_value="THE EVOLUTION") as call, \
                mock.patch("scripts.research.rebuild_summary") as rebuild:
            result = SP.run_second_pass(
                self.json_path, primary_type="single_name", subject="FAKE")
        self.assertEqual(result["status"], "ok")
        call.assert_called_once()
        rebuild.assert_called_once_with("FAKE")
        saved = json.loads(self.json_path.read_text(encoding="utf-8"))
        self.assertIn("THE EVOLUTION", saved["analysis_report"])
        self.assertNotIn("PENDING_SECOND_PASS", saved["analysis_report"])
        md = self.json_path.with_name("2026-07-13_note.pdf_summary.md")
        self.assertIn("THE EVOLUTION", md.read_text(encoding="utf-8"))
        # Second run is a no-op.
        with mock.patch("scripts.research._call_api") as call2:
            again = SP.run_second_pass(
                self.json_path, primary_type="single_name", subject="FAKE")
        self.assertEqual(again["status"], "already_done")
        call2.assert_not_called()

    def test_macro_second_pass_rebuilds_author_then_category(self):
        self._write_note("PENDING_SECOND_PASS")
        with mock.patch("scripts.research.Anthropic"), \
                mock.patch("scripts.research._call_api", return_value="EVO"), \
                mock.patch("scripts.macro.use_category") as use_cat, \
                mock.patch("scripts.macro._load_author_summary",
                           return_value={"views": {}, "recommended_trades_history": []}), \
                mock.patch("scripts.macro._macro_dir", return_value=Path(self._tmp.name)), \
                mock.patch("scripts.macro._rebuild_author_summary") as rebuild_author, \
                mock.patch("scripts.macro._update_themes") as update_themes, \
                mock.patch("scripts.macro._rebuild_macro_summary") as rebuild_macro:
            result = SP.run_second_pass(
                self.json_path, primary_type="macro", subject="Some Author",
                category="Semis")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            [c.args[0] for c in use_cat.call_args_list], ["Semis", "Semis"])
        rebuild_author.assert_called_once()
        update_themes.assert_called_once()
        rebuild_macro.assert_called_once()


if __name__ == "__main__":
    unittest.main()
