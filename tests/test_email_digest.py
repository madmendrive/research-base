"""Email sweep digest: per-item list, key lookup, and analyse_email job."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import email_sweep as E


class TestEmailDigest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        p = mock.patch.object(E, "EMAIL_DIR", self.root); p.start(); self.addCleanup(p.stop)

    def _items(self):
        md = self.root / "msg.md"
        md.write_text("# Subj\n\nMemory pricing is inflecting per the note.", encoding="utf-8")
        return [
            {"key": "k1", "subject": "SK hynix HBM4 ramp", "sender": "JPM",
             "md_path": str(md), "structured": True},
            {"key": "k2", "subject": "Substrate supply note", "sender": "Bernstein",
             "md_path": str(md), "structured": False},
        ]

    def test_digest_format_includes_analyse_commands(self):
        items = [dict(it, rank=i) for i, it in enumerate(self._items(), 1)]
        out = E._format_email_digest(items, {"queued_pdfs": 0})
        self.assertIn("/email_1", out)
        self.assertIn("/email_2", out)
        self.assertIn("SK hynix HBM4 ramp", out)
        self.assertIn("structured", out)  # k1 flagged

    def test_save_and_lookup_by_key_and_rank(self):
        items = [dict(it, rank=i) for i, it in enumerate(self._items(), 1)]
        E._save_latest_email_digest(items)
        self.assertEqual(E.get_email("k2")["subject"], "Substrate supply note")
        self.assertEqual(E.get_email_by_rank(1)["key"], "k1")
        self.assertIsNone(E.get_email("nope"))
        self.assertIsNone(E.get_email_by_rank(99))

    def test_analyse_email_runs_readthrough_and_notifies(self):
        items = [dict(it, rank=i) for i, it in enumerate(self._items(), 1)]
        E._save_latest_email_digest(items)
        sent = []
        with mock.patch("scripts.analyst.email_readthrough", return_value="READTHROUGH") as rt, \
             mock.patch.object(E.kb, "index_text"), \
             mock.patch.object(E, "telegram_send_markdownish_html", side_effect=lambda t: sent.append(t)):
            result = E.analyse_email("k1", notify=True)
        rt.assert_called_once()
        # body from md_path was passed through
        self.assertIn("Memory pricing", rt.call_args.kwargs.get("body", ""))
        self.assertEqual(sent, ["READTHROUGH"])
        self.assertEqual(result["key"], "k1")

    def test_analyse_email_unknown_key_raises(self):
        E._save_latest_email_digest([])
        with self.assertRaises(FileNotFoundError):
            E.analyse_email("ghost")


if __name__ == "__main__":
    unittest.main()
