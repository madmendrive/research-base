"""Recency window: dated items use publish date; undated items fall back to
first-seen time so stale headlines re-scraped off index pages are filtered."""

import unittest
from datetime import datetime, timedelta, timezone

from scripts.headlines import _within_window


class TestWithinWindow(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)

    def iso(self, hours_ago):
        return (self.now - timedelta(hours=hours_ago)).isoformat(timespec="seconds")

    def test_dated_fresh_passes(self):
        self.assertTrue(_within_window(self.iso(5), 24, self.now))

    def test_dated_stale_filtered(self):
        self.assertFalse(_within_window(self.iso(72), 24, self.now))

    def test_undated_recent_first_seen_passes(self):
        # No publish date, but we first saw it 2h ago -> fresh.
        self.assertTrue(_within_window("", 24, self.now, fallback=self.iso(2)))

    def test_undated_old_first_seen_filtered(self):
        # The reported bug: undated headline re-appearing for days.
        self.assertFalse(_within_window("", 24, self.now, fallback=self.iso(120)))

    def test_no_signal_at_all_kept(self):
        # Neither publish date nor first-seen -> can't reject, keep.
        self.assertTrue(_within_window("", 24, self.now, fallback=""))

    def test_published_date_wins_over_fallback(self):
        # Stale publish date filters even if first-seen is recent.
        self.assertFalse(_within_window(self.iso(100), 24, self.now, fallback=self.iso(1)))


if __name__ == "__main__":
    unittest.main()
