"""Batch cross-cut backfill: request building, chunking, apply + state marking."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import batch_cross_cut as BC


def _pair(kind="ticker", target="FAKE", digest="a" * 64, src="stored.pdf"):
    return {"file_hash": digest, "file": "x.pdf", "source_path": src,
            "kind": kind, "target": target, "source_exists": True}


class TestRequestBuilding(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name)
        p = mock.patch.object(BC, "DATA_DIR", self.data)
        p.start()
        self.addCleanup(p.stop)
        # Stored doc + its extraction JSON
        self.src = self.data / "FAKE" / "research" / "notes" / "2026-06-09_x.pdf"
        self.src.parent.mkdir(parents=True)
        self.src.write_bytes(b"%PDF")
        (self.data / "FAKE" / "research" / "summary.json").write_text(
            json.dumps({"consensus_estimates": {"rev": 1}}), encoding="utf-8")
        Path(str(self.src) + ".json").write_text(
            json.dumps({"metadata": {"title": "T"}, "detailed_summary": "s"}),
            encoding="utf-8")

    def test_ticker_request_carries_extraction_summary_and_doc_block(self):
        pair = _pair(src=str(self.src))
        cache = {str(self.src): "DOC TEXT HERE"}
        with mock.patch("scripts.research._load_companies",
                        return_value={"FAKE": {"name": "Fake Corp"}}):
            req = BC._build_request(pair, cache)
        self.assertIsNotNone(req)
        prompt = req["params"]["messages"][0]["content"]
        self.assertIn("Fake Corp", prompt)
        self.assertIn("structured extraction", prompt)
        self.assertIn('"rev": 1', prompt)
        (block,) = req["params"]["system"]
        self.assertIn("DOC TEXT HERE", block["text"])
        self.assertEqual(block["cache_control"], {"type": "ephemeral"})

    def test_unknown_target_or_empty_doc_is_skipped(self):
        cache = {str(self.src): "DOC"}
        with mock.patch("scripts.research._load_companies", return_value={}):
            self.assertIsNone(BC._build_request(_pair(src=str(self.src)), cache))
        # No extraction and no doc text -> nothing to analyse from.
        bare = self.data / "bare.pdf"
        bare.write_bytes(b"%PDF")
        with mock.patch("scripts.research._load_companies",
                        return_value={"FAKE": {"name": "Fake Corp"}}):
            self.assertIsNone(BC._build_request(_pair(src=str(bare)), {str(bare): ""}))


class TestPromptCaps(unittest.TestCase):
    def test_theme_prompt_caps_linked_summaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            theme_dir = data / "Thematic" / "TestTheme"
            theme_dir.mkdir(parents=True)
            (theme_dir / "linked_tickers.json").write_text(json.dumps({
                "theme": "TestTheme",
                "linked_tickers": [{"ticker": "BIG", "relevance": "r"},
                                   {"ticker": "SMALL", "relevance": "r"}],
            }), encoding="utf-8")
            big = data / "BIG" / "research"
            big.mkdir(parents=True)
            (big / "summary.json").write_text(
                json.dumps({"pad": "x" * 200_000}), encoding="utf-8")
            small = data / "SMALL" / "research"
            small.mkdir(parents=True)
            (small / "summary.json").write_text(json.dumps({"ok": 1}), encoding="utf-8")
            import scripts.thematic as thematic
            with mock.patch.object(BC, "DATA_DIR", data), \
                    mock.patch.object(thematic, "THEMATIC_DIR", data / "Thematic"), \
                    mock.patch("scripts.research._load_companies",
                               return_value={"BIG": {"name": "Big"}, "SMALL": {"name": "Small"}}):
                prompt = BC._theme_prompt("TestTheme", extraction="{}")
        self.assertIsNotNone(prompt)
        self.assertIn("[...truncated]", prompt)
        self.assertIn('"ok": 1', prompt)
        # The 200KB summary must not survive whole.
        self.assertLess(len(prompt), 40_000)

    def test_oversized_request_aborts_submit(self):
        pairs = [_pair()]
        big_req = {"custom_id": BC._custom_id(pairs[0]),
                   "params": {"messages": [{"content": "x" * 1000}]}}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(BC, "STATE_PATH", Path(tmp) / "s.json"), \
                mock.patch.object(BC, "MAX_REQUEST_BYTES", 500), \
                mock.patch.object(BC, "pending_pairs", return_value=pairs), \
                mock.patch.object(BC, "_build_request", return_value=big_req), \
                mock.patch.object(BC, "_client") as client:
            with self.assertRaises(RuntimeError):
                BC.submit_batches()
            client.assert_not_called()  # aborted before any upload


class TestAnalysisPathLength(unittest.TestCase):
    def test_long_stems_are_capped_and_unique(self):
        from scripts.kb import capped_stem

        long_a = "2026-07-15_Weekly_" + "Meta Capex Defies Cut Fears " * 8 + "A"
        long_b = long_a[:-1] + "B"  # same long prefix, different tail
        a, b = capped_stem(long_a), capped_stem(long_b)
        self.assertLessEqual(len(a), 70 + 7)
        self.assertNotEqual(a, b)  # hash suffix disambiguates shared prefixes
        self.assertEqual(capped_stem("short"), "short")

        item = {"source_path": rf"C:\x\notes\{long_a}.pdf",
                "kind": "theme", "target": "AI Infrastructure"}
        path = BC._analysis_path(item)
        self.assertLess(len(str(path)), 220)


class TestSubmitChunking(unittest.TestCase):
    def test_requests_split_when_size_cap_exceeded(self):
        pairs = [_pair(digest=str(i) * 64, target=f"T{i}") for i in range(3)]
        made = []

        def fake_create(requests):
            made.append(len(requests))
            return SimpleNamespace(id=f"b{len(made)}", processing_status="in_progress")

        client = mock.Mock()
        client.messages.batches.create.side_effect = lambda requests: fake_create(requests)
        fake_req = {"custom_id": None, "params": {"messages": [{"content": "x" * 100}]}}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(BC, "STATE_PATH", Path(tmp) / "s.json"), \
                mock.patch.object(BC, "MAX_BATCH_BYTES", 250), \
                mock.patch.object(BC, "pending_pairs", return_value=pairs), \
                mock.patch.object(BC, "_build_request",
                                  side_effect=lambda p, c: {**fake_req, "custom_id": BC._custom_id(p)}), \
                mock.patch.object(BC, "_client", return_value=client):
            stats = BC.submit_batches()
            state = json.loads(BC.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(stats["requests"], 3)
        self.assertGreater(len(stats["batches"]), 1)  # size cap forced a split
        total_items = sum(len(b["items"]) for b in state["batches"].values())
        self.assertEqual(total_items, 3)


class TestApply(unittest.TestCase):
    def test_apply_writes_analysis_and_marks_shared_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            pair = _pair(src=str(data / "notes" / "2026-06-09_x.pdf"))
            cid = BC._custom_id(pair)
            cc_path = data / "_bulk_cross_cut_state.json"
            result = SimpleNamespace(
                custom_id=cid,
                result=SimpleNamespace(type="succeeded", message=SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="THE CROSS CUT")])))
            client = mock.Mock()
            client.messages.batches.retrieve.return_value = SimpleNamespace(
                processing_status="ended")
            client.messages.batches.results.return_value = iter([result])
            with mock.patch.object(BC, "DATA_DIR", data), \
                    mock.patch.object(BC, "STATE_PATH", data / "_batches.json"), \
                    mock.patch("scripts.bulk_cross_cut.CC_STATE_PATH", cc_path), \
                    mock.patch.object(BC, "_client", return_value=client):
                BC._save_state({"batches": {"b1": {
                    "submitted_at": "2026-07-13T00:00:00+00:00",
                    "items": {cid: {k: pair[k] for k in
                                    ("file_hash", "file", "source_path", "kind", "target")}},
                }}})
                out = BC.apply_batches()
                self.assertEqual(out["applied"], 1)
                md = data / "FAKE" / "research" / "analyses"
                (analysis,) = list(md.glob("*_analysis.md"))
                self.assertIn("THE CROSS CUT", analysis.read_text(encoding="utf-8"))
                cc = json.loads(cc_path.read_text(encoding="utf-8"))
                key = f"{pair['file_hash']}:ticker:FAKE"
                self.assertIn(key, cc["processed_pairs"])
                self.assertEqual(cc["processed_pairs"][key]["via"], "batch")
                # Re-apply: batch already marked applied — nothing double-written.
                client.messages.batches.results.return_value = iter([result])
                again = BC.apply_batches()
                self.assertEqual(again["applied"], 0)


if __name__ == "__main__":
    unittest.main()
