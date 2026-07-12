"""Fast-path commit parity with the classic pipeline: routing log,
held-for-review notices, theme proposals, and summary rebuilds."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import parallel_ingest as P


def _stage_record(tmp: Path, triage: dict, extraction: dict | None = None) -> Path:
    source = tmp / "inbox" / "note.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"%PDF-1.4 fake")
    record = {
        "digest": "d" * 64,
        "source_path": str(source),
        "file": source.name,
        "size": source.stat().st_size,
        "triage": triage,
        "extraction": extraction or {"metadata": {"title": "T"}, "detailed_summary": "s", "analysis_report": ""},
        "staged_at": "2026-07-12T00:00:00+00:00",
    }
    stage_path = tmp / "_staging_ingest" / f"{record['digest']}.json"
    stage_path.parent.mkdir(parents=True, exist_ok=True)
    stage_path.write_text(json.dumps(record), encoding="utf-8")
    return stage_path


class TestCommitStageParity(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        for name, value in {
            "PROJECT_ROOT": self.tmp,
            "DATA_DIR": self.tmp / "data",
            "STAGING_DIR": self.tmp / "data" / "_staging_ingest",
            "PENDING_REVIEW_DIR": self.tmp / "data" / "_pending_review",
            "ROUTING_LOG_PATH": self.tmp / "data" / "_routing_log.jsonl",
            "THEME_PROPOSALS_PATH": self.tmp / "data" / "_theme_proposals.jsonl",
            # Identity: the fake subjects in these tests are not real tickers.
            "canonicalize_ticker": lambda s: s,
        }.items():
            p = mock.patch.object(P, name, value)
            p.start()
            self.addCleanup(p.stop)
        for target in ("_auto_add_tickers", "_auto_add_authors"):
            p = mock.patch.object(P, target)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(P.kb, "index_file", return_value={"status": "indexed"})
        self.index_file = p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(P.kb, "connect")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch("scripts.research_memory.ingest_file")
        p.start()
        self.addCleanup(p.stop)

    def _routing_lines(self):
        text = P.ROUTING_LOG_PATH.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def test_held_file_logs_routing_and_sends_buttoned_notice(self):
        triage = {
            "primary_type": "single_name", "primary_subject": "FAKE",
            "confidence": "low", "rationale": "unsure",
            "tickers_covered": ["FAKE"], "themes_touched": [],
        }
        stage_path = _stage_record(self.tmp, triage)
        with mock.patch("scripts.notify.telegram_send_with_buttons", return_value=True) as send:
            row = P._commit_stage(stage_path)
        self.assertEqual(row["status"], "pending_review")
        self.assertIsNone(row["json_path"])
        # Held copy landed in _pending_review with today's date prefix.
        held = list(P.PENDING_REVIEW_DIR.glob("*_note.pdf"))
        self.assertEqual(len(held), 1)
        # Routing log records the hold, marked as the fast pipeline.
        (line,) = self._routing_lines()
        self.assertEqual(line["pipeline"], "fast")
        self.assertEqual(line["confidence"], "low")
        self.assertEqual(line["routed_to"], "data/_pending_review")
        # Notice carries the same Confirm/Reclassify/Drop buttons /pending uses.
        send.assert_called_once()
        (buttons,) = send.call_args.args[1]
        self.assertEqual(
            [b["callback_data"].split(":")[0] for b in buttons], ["pc", "pr", "pd"])

    def test_stored_file_logs_routing_and_rebuilds_summary(self):
        triage = {
            "primary_type": "single_name", "primary_subject": "FAKE",
            "confidence": "high", "tickers_covered": ["FAKE"], "themes_touched": [],
            "proposed_new_themes": ["Robotaxis"],
        }
        stage_path = _stage_record(self.tmp, triage)
        with mock.patch("scripts.research.rebuild_summary") as rebuild, \
                mock.patch("scripts.notify.telegram_send", return_value=True) as send:
            row = P._commit_stage(stage_path)
        self.assertEqual(row["status"], "research")
        self.assertIsNone(row["summary_rebuild_error"])
        rebuild.assert_called_once_with("FAKE")
        (line,) = self._routing_lines()
        self.assertEqual(line["routed_to"], "data/FAKE/research/notes")
        # Theme proposal is persisted and announced.
        (prop,) = [json.loads(x) for x in
                   P.THEME_PROPOSALS_PATH.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(prop["themes"], ["Robotaxis"])
        send.assert_called_once()
        self.assertIn("Robotaxis", send.call_args.args[0])

    def test_summary_rebuild_failure_does_not_fail_the_commit(self):
        triage = {
            "primary_type": "single_name", "primary_subject": "FAKE",
            "confidence": "high", "tickers_covered": [], "themes_touched": [],
        }
        stage_path = _stage_record(self.tmp, triage)
        with mock.patch("scripts.research.rebuild_summary", side_effect=KeyError("FAKE")):
            row = P._commit_stage(stage_path)
        self.assertEqual(row["status"], "research")
        self.assertIn("KeyError", row["summary_rebuild_error"])

    def test_notify_false_suppresses_telegram_but_keeps_audit_trail(self):
        triage = {
            "primary_type": "single_name", "primary_subject": "FAKE",
            "confidence": "low", "rationale": "unsure",
            "tickers_covered": [], "themes_touched": [],
            "proposed_new_themes": ["Robotaxis"],
        }
        stage_path = _stage_record(self.tmp, triage)
        with mock.patch("scripts.notify.telegram_send_with_buttons") as send_buttons, \
                mock.patch("scripts.notify.telegram_send") as send:
            P._commit_stage(stage_path, notify=False)
        send_buttons.assert_not_called()
        send.assert_not_called()
        self.assertEqual(len(self._routing_lines()), 1)
        self.assertTrue(P.THEME_PROPOSALS_PATH.exists())

    def test_macro_rebuild_uses_category_context(self):
        triage = {
            "primary_type": "macro", "primary_subject": "Some Author",
            "category": "Semis", "confidence": "high",
            "tickers_covered": [], "themes_touched": [],
        }
        stage_path = _stage_record(self.tmp, triage)
        with mock.patch("scripts.macro.use_category") as use_cat, \
                mock.patch("scripts.macro._rebuild_macro_summary") as rebuild, \
                mock.patch("scripts.authors.canonicalize_author", side_effect=lambda a: a):
            P._commit_stage(stage_path, notify=False)
        use_cat.assert_called_once_with("Semis")
        rebuild.assert_called_once()


if __name__ == "__main__":
    unittest.main()
