"""Fresh-only: headlines already delivered in a prior brief are excluded from
later briefs; only delivered items get marked digested."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from scripts import headlines as H


class TestFreshOnly(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.state_path = root / "state.json"
        for tgt, val in [("HEADLINE_DIR", root), ("STATE_PATH", self.state_path),
                         ("LATEST_DIGEST_PATH", root / "latest.json")]:
            p = mock.patch.object(H, tgt, val); p.start(); self.addCleanup(p.stop)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.itemA = {"title": "Already sent story", "url": "http://x/a",
                      "source": "Reuters", "published_at": now}
        self.itemB = {"title": "Brand new story", "url": "http://x/b",
                      "source": "Reuters", "published_at": now}
        self.keyA = H._headline_key(self.itemA)
        self.keyB = H._headline_key(self.itemB)

        # Patch collaborators so only the fresh-only filter is exercised.
        self.sent = []
        patches = [
            mock.patch.object(H, "_load_config", return_value={}),
            mock.patch.object(H, "_source_list", return_value=["reuters"]),
            mock.patch.object(H, "_terms", return_value=["ai"]),
            mock.patch.object(H, "_fetch_native_headlines",
                              return_value=([self.itemA, self.itemB], {"reuters": {"items": 2}})),
            mock.patch.object(H, "_score_item", side_effect=lambda it: (10, {**it, "score": 10, "entities": {}})),
            mock.patch.object(H, "_is_digest_candidate", return_value=True),
            mock.patch.object(H, "_enrich_digest_items_with_article_text", side_effect=lambda x: x),
            mock.patch.object(H, "_tech_brief_rows_with_claude", return_value=[]),
            mock.patch.object(H, "_format_markdown_brief", side_effect=lambda items, rows: "digest"),
            mock.patch.object(H, "_format_telegram_brief", side_effect=lambda items, rows, wh: "html"),
            mock.patch.object(H, "_save_latest_digest"),
            mock.patch.object(H.kb, "index_text"),
            mock.patch.object(H, "telegram_send", side_effect=lambda *a, **k: self.sent.append((a, k))),
            mock.patch.dict("os.environ", {"HEADLINE_GOOGLE_FALLBACK": "0", "HEADLINE_FRESH_ONLY": "1"}),
        ]
        for p in patches:
            p.start(); self.addCleanup(p.stop)

    def _digested_keys(self):
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        return {k for k, v in state.get("seen", {}).items() if (v or {}).get("digested_at")}

    def test_new_items_delivered_and_marked(self):
        stats = H.headline_sweep(notify=True, window_hours=24, max_digest_items=20)
        self.assertEqual(stats["digest_items"], 2)
        self.assertEqual(self._digested_keys(), {self.keyA, self.keyB})

    def test_already_digested_item_excluded(self):
        # Pre-seed: itemA already delivered in a prior brief.
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.state_path.write_text(json.dumps({"seen": {
            self.keyA: {"first_seen_at": now, "digested_at": now}}}), encoding="utf-8")
        stats = H.headline_sweep(notify=True, window_hours=24, max_digest_items=20)
        # Only the new item B should be in the digest.
        self.assertEqual(stats["digest_items"], 1)
        self.assertIn(self.keyB, self._digested_keys())

    def test_notify_off_does_not_mark_digested(self):
        H.headline_sweep(notify=False, window_hours=24, max_digest_items=20)
        # Nothing delivered -> nothing suppressed from a later real brief.
        self.assertEqual(self._digested_keys(), set())


if __name__ == "__main__":
    unittest.main()
