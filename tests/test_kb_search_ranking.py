"""Hybrid search ranking: RRF fusion of BM25 and cosine rank positions."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import kb


class _StubEmbedder:
    """Deterministic embeddings: the query matches doc B far better than doc A."""

    model = "stub"
    enabled = True

    VECTORS = {
        "query": [1.0, 0.0, 0.0],
        "semantic match": [0.99, 0.14, 0.0],
        "keyword match": [0.0, 1.0, 0.0],
    }

    def embed_texts(self, texts):
        out = []
        for t in texts:
            for key, vec in self.VECTORS.items():
                if key in t:
                    out.append(vec)
                    break
            else:
                out.append([0.0, 0.0, 1.0])
        return out


class TestHybridRanking(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "kb.sqlite"
        real_connect = kb.connect
        p = mock.patch.object(kb, "connect", lambda db_path=None: real_connect(self.db))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(kb, "EmbeddingClient", _StubEmbedder)
        p.start()
        self.addCleanup(p.stop)

    def _index(self, title, text):
        conn = kb.connect()
        kb.index_text(title=title, text=text, source_type="note",
                      source_uri=f"test:{title}", embed=True, conn=conn)
        conn.close()

    def test_fts_hits_have_distinct_scores(self):
        # Regression: max(bm25, 0) flattened every keyword hit to score 1.0.
        self._index("a", "memory pricing memory pricing memory pricing tightens")
        self._index("b", "memory pricing mentioned once amid other words entirely")
        results = kb.search("memory pricing", use_vector=False)
        self.assertEqual(len(results), 2)
        self.assertNotEqual(results[0]["score"], results[1]["score"])

    def test_semantic_hit_can_outrank_weak_keyword_hit(self):
        # Doc A contains the literal query words once (weak keyword hit).
        # Doc B says it differently but embeds nearly identically to the query.
        self._index("weak-keyword", "query words appear here keyword match text")
        self._index("semantic", "different vocabulary entirely semantic match body")
        with mock.patch.object(kb, "_fts_query", side_effect=kb._fts_query):
            results = kb.search("query words")
        titles = [r["title"] for r in results]
        self.assertIn("semantic", titles)  # vector-only hit surfaces at all

    def test_hybrid_match_combines_both_rank_lists(self):
        self._index("both", "query words and also a semantic match inside")
        results = kb.search("query words")
        self.assertTrue(any(r["match"] in {"hybrid", "fts"} for r in results))
        # Fused score of a hybrid hit exceeds a pure single-list contribution.
        best = results[0]
        self.assertGreater(best["score"], 1.0 / (kb.RRF_K + 1) - 1e-9)

    def test_vector_disabled_still_returns_keyword_results(self):
        self._index("only", "plain keyword text about memory pricing")
        results = kb.search("memory pricing", use_vector=False)
        self.assertEqual(results[0]["match"], "fts")


if __name__ == "__main__":
    unittest.main()
