"""Combined sweep digest: coordinator interleavings, failsafe, late parts."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from scripts import combined_digest as C


class TestCombinedDigest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        for name, value in {
            "STATE_PATH": root / "state.json",
            "LOCK_PATH": root / "state.lock",
        }.items():
            p = mock.patch.object(C, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.sent = []
        p = mock.patch.object(
            C, "telegram_send_markdownish_html",
            side_effect=lambda t: self.sent.append(t) or True,
        )
        p.start()
        self.addCleanup(p.stop)

    def _age_state(self, minutes):
        rec = json.loads(C.STATE_PATH.read_text(encoding="utf-8"))
        old = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(
            timespec="seconds"
        )
        rec["created_at"] = old
        rec["updated_at"] = old
        C.STATE_PATH.write_text(json.dumps(rec), encoding="utf-8")

    def test_both_parts_send_one_combined_message(self):
        C.begin_day()
        self.assertFalse(C.submit_part("email", "EMAIL DIGEST"))
        self.assertTrue(C.submit_part("inbox", "INBOX DIGEST"))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("EMAIL DIGEST", self.sent[0])
        self.assertIn("INBOX DIGEST", self.sent[0])
        rec = json.loads(C.STATE_PATH.read_text(encoding="utf-8"))
        self.assertTrue(rec["sent"])
        self.assertEqual(rec["sent_parts"], ["email", "inbox"])

    def test_flush_does_not_fire_before_stale(self):
        C.begin_day()
        C.submit_part("email", "EMAIL DIGEST")
        self.assertFalse(C.flush_if_stale())
        self.assertEqual(self.sent, [])

    def test_part_submission_resets_staleness_clock(self):
        C.begin_day()
        self._age_state(C.STALE_MINUTES + 10)
        # A part arriving refreshes updated_at, so the flusher backs off.
        C.submit_part("email", "EMAIL DIGEST")
        self.assertFalse(C.flush_if_stale())
        self.assertEqual(self.sent, [])

    def test_stale_flush_sends_partial_with_placeholder(self):
        C.begin_day()
        C.submit_part("email", "EMAIL DIGEST")
        self._age_state(C.STALE_MINUTES + 1)
        with mock.patch.object(C, "_recover_inbox_section", return_value=""):
            self.assertTrue(C.flush_if_stale())
        self.assertEqual(len(self.sent), 1)
        self.assertIn("EMAIL DIGEST", self.sent[0])
        self.assertIn("not finished yet", self.sent[0])

    def test_late_part_after_flush_sent_as_followup_not_discarded(self):
        # Regression: the completed inbox digest used to be silently dropped
        # if the failsafe had already flushed a partial.
        C.begin_day()
        C.submit_part("email", "EMAIL DIGEST")
        self._age_state(C.STALE_MINUTES + 1)
        with mock.patch.object(C, "_recover_inbox_section", return_value=""):
            C.flush_if_stale()
        self.assertFalse(C.submit_part("inbox", "REAL INBOX DIGEST"))
        self.assertEqual(len(self.sent), 2)
        self.assertIn("REAL INBOX DIGEST", self.sent[1])
        self.assertIn("(late)", self.sent[1])

    def test_no_duplicate_combined_send_after_flush(self):
        # Regression: submit_part used to clobber sent=False from a stale
        # in-memory record and re-send the combined digest.
        C.begin_day()
        C.submit_part("email", "EMAIL DIGEST")
        self._age_state(C.STALE_MINUTES + 1)
        with mock.patch.object(C, "_recover_inbox_section", return_value=""):
            C.flush_if_stale()
        C.submit_part("inbox", "INBOX")
        C.submit_part("inbox", "INBOX")  # resubmission: already in sent_parts
        # EMAIL DIGEST only ever goes out once (in the flush); the late part
        # follow-up must not re-send the combined message.
        self.assertEqual(sum("EMAIL DIGEST" in m for m in self.sent), 1)
        self.assertEqual(len(self.sent), 2)  # flush + one late follow-up

    def test_flush_with_zero_parts_sends_placeholders(self):
        C.begin_day()
        self._age_state(C.STALE_MINUTES + 1)
        with mock.patch.object(C, "_recover_inbox_section", return_value=""):
            self.assertTrue(C.flush_if_stale())
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0].count("not finished yet"), 2)

    def test_recovered_inbox_still_allows_real_late_part(self):
        C.begin_day()
        C.submit_part("email", "EMAIL DIGEST")
        self._age_state(C.STALE_MINUTES + 1)
        with mock.patch.object(C, "_recover_inbox_section", return_value="RECOVERED 2 of 5"):
            C.flush_if_stale()
        self.assertIn("RECOVERED 2 of 5", self.sent[0])
        C.submit_part("inbox", "FULL INBOX DIGEST")
        self.assertEqual(len(self.sent), 2)
        self.assertIn("FULL INBOX DIGEST", self.sent[1])

    def test_delivery_failure_recorded_and_not_resent(self):
        C.telegram_send_markdownish_html.side_effect = lambda t: False
        C.begin_day()
        C.submit_part("email", "E")
        C.submit_part("inbox", "I")
        rec = json.loads(C.STATE_PATH.read_text(encoding="utf-8"))
        self.assertTrue(rec["sent"])  # claim-first: no duplicate risk
        self.assertFalse(rec["last_delivery_ok"])

    def test_corrupt_state_file_starts_fresh(self):
        C.STATE_PATH.write_text("{not json", encoding="utf-8")
        self.assertFalse(C.submit_part("email", "EMAIL DIGEST"))
        rec = json.loads(C.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(list(rec["parts"]), ["email"])

    def test_atomic_save_leaves_no_tmp(self):
        C.begin_day()
        self.assertTrue(C.STATE_PATH.exists())
        self.assertFalse(C.STATE_PATH.with_suffix(".tmp").exists())

    def test_stale_lockfile_is_broken(self):
        C.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        C.LOCK_PATH.write_text("9999", encoding="utf-8")
        import os
        old = C.LOCK_PATH.stat()
        os.utime(C.LOCK_PATH, (old.st_atime - 120, old.st_mtime - 120))
        C.begin_day()  # must not hang or time out
        self.assertTrue(C.STATE_PATH.exists())


if __name__ == "__main__":
    unittest.main()
