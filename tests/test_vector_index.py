"""Float32-BLOB embeddings, JSON->BLOB migration, sidecar full-corpus search."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from scripts import kb


class _StubEmbedder:
    """Deterministic 3-dim embeddings keyed by marker words in the text."""

    model = "stub"
    enabled = True

    VECTORS = {
        "query": [1.0, 0.0, 0.0],
        "semantic match": [0.98, 0.199, 0.0],
        "keyword match": [0.0, 1.0, 0.0],
        "unrelated": [0.0, 0.0, 1.0],
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


class VecCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.db = root / "kb.sqlite"
        real_connect = kb.connect
        for name, value in {
            "VEC_INDEX_DIR": root / "vec_index",
            "VEC_POINTER": root / "vec_index" / "current.json",
        }.items():
            p = mock.patch.object(kb, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(kb, "connect", lambda db_path=None: real_connect(self.db))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(kb, "EmbeddingClient", _StubEmbedder)
        p.start()
        self.addCleanup(p.stop)
        kb._vec_cache.clear()
        self.addCleanup(kb._vec_cache.clear)

    def _index(self, title, text, embed=True):
        conn = kb.connect()
        kb.index_text(title=title, text=text, source_type="note",
                      source_uri=f"test:{title}", embed=embed, conn=conn)
        conn.close()


class TestBlobRoundtrip(unittest.TestCase):
    def test_vec_blob_roundtrip_float32(self):
        vec = [0.123456789, -1.5, 0.0, 3.25]
        out = kb.blob_to_vec(kb.vec_to_blob(vec))
        self.assertEqual(out.dtype, np.dtype("<f4"))
        np.testing.assert_allclose(out, vec, rtol=1e-6)

    def test_row_embedding_prefers_blob(self):
        row = {"embedding": kb.vec_to_blob([1.0, 2.0]), "embedding_json": "[9.0, 9.0]"}
        np.testing.assert_allclose(kb._row_embedding(row), [1.0, 2.0])

    def test_row_embedding_falls_back_to_json(self):
        row = {"embedding": None, "embedding_json": "[3.0, 4.0]"}
        self.assertEqual(kb._row_embedding(row), [3.0, 4.0])


class TestWriteAndMigrate(VecCase):
    def _add_legacy_column(self):
        """Fresh DBs no longer carry embedding_json; simulate a legacy DB."""
        conn = kb.connect()
        conn.execute("ALTER TABLE chunks ADD COLUMN embedding_json TEXT")
        conn.commit()
        conn.close()

    def test_index_text_writes_blob(self):
        self._index("a", "some keyword match text")
        conn = kb.connect()
        row = conn.execute("SELECT embedding FROM chunks").fetchone()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)").fetchall()}
        conn.close()
        self.assertIsNotNone(row["embedding"])
        self.assertNotIn("embedding_json", cols)  # fresh schema is blob-only

    def test_embed_migrate_converts_legacy_json_rows(self):
        self._add_legacy_column()
        self._index("a", "keyword match body", embed=False)
        conn = kb.connect()
        conn.execute("UPDATE chunks SET embedding_json = ?", (json.dumps([0.0, 1.0, 0.0]),))
        conn.commit()
        conn.close()
        stats = kb.embed_migrate()
        self.assertEqual(stats["converted"], 1)
        self.assertEqual(stats["remaining"], 0)
        # Idempotent: second run converts nothing.
        self.assertEqual(kb.embed_migrate()["converted"], 0)
        conn = kb.connect()
        row = conn.execute("SELECT embedding, embedding_json FROM chunks").fetchone()
        conn.close()
        np.testing.assert_allclose(kb.blob_to_vec(row["embedding"]), [0.0, 1.0, 0.0])
        self.assertIsNotNone(row["embedding_json"])  # kept until the drop step

    def test_embed_migrate_noop_without_legacy_column(self):
        self._index("a", "keyword match body")
        stats = kb.embed_migrate()
        self.assertEqual(stats["converted"], 0)
        self.assertFalse(stats["column_present"])

    def test_unembedded_counts(self):
        self._index("a", "keyword match body")   # blob
        self._index("b", "no embedding here", embed=False)
        stats = kb.embed_backfill(dry_run=True)
        self.assertEqual(stats["unembedded"], 1)  # only the embed=False row

    def test_backfill_migrates_legacy_json_instead_of_reembedding(self):
        # A json-only row must be converted for free, never re-embedded.
        self._add_legacy_column()
        self._index("a", "keyword match body", embed=False)
        conn = kb.connect()
        conn.execute("UPDATE chunks SET embedding_json = ?", (json.dumps([0.0, 1.0, 0.0]),))
        conn.commit()
        conn.close()
        stats = kb.embed_backfill(dry_run=True)  # dry run still migrates first
        self.assertEqual(stats["unembedded"], 0)
        conn = kb.connect()
        row = conn.execute("SELECT embedding FROM chunks").fetchone()
        conn.close()
        self.assertIsNotNone(row["embedding"])

    def test_drop_refuses_with_unconverted_rows_then_drops_clean(self):
        self._add_legacy_column()
        self._index("a", "keyword match body", embed=False)
        conn = kb.connect()
        conn.execute("UPDATE chunks SET embedding_json = ?", (json.dumps([1.0, 0.0, 0.0]),))
        conn.commit()
        conn.close()
        refused = kb.drop_embedding_json()
        self.assertFalse(refused["dropped"])
        self.assertIn("unconverted", refused["reason"])
        kb.embed_migrate()
        dropped = kb.drop_embedding_json()
        self.assertTrue(dropped["dropped"])
        conn = kb.connect()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)").fetchall()}
        conn.close()
        self.assertNotIn("embedding_json", cols)
        # Second call no-ops; search still works post-drop.
        self.assertFalse(kb.drop_embedding_json()["dropped"])
        self.assertEqual(kb.search("keyword match", use_vector=False)[0]["title"], "a")


class TestSidecarSearch(VecCase):
    def test_build_creates_pointer_and_normalized_matrix(self):
        self._index("a", "keyword match text")
        meta = kb.build_vector_index()
        self.assertTrue(meta["built"])
        self.assertEqual(meta["count"], 1)
        loaded = kb._load_vector_index()
        ids, vecs, _ = loaded
        self.assertEqual(len(ids), 1)
        np.testing.assert_allclose(np.linalg.norm(vecs[0]), 1.0, rtol=1e-5)

    def test_full_scan_finds_semantic_hit_beyond_recent_pool(self):
        # Regression for the recall ceiling: a semantically similar chunk that
        # (a) shares no keywords with the query and (b) is NOT among the most
        # recent chunks was previously unreachable. Bury the semantic doc
        # under newer unrelated chunks deeper than the recent-pool window.
        self._index("target", "different vocabulary entirely semantic match body")
        with mock.patch.object(kb, "VECTOR_POOL_RECENT_CHUNKS", 2):
            for i in range(3):
                self._index(f"noise{i}", f"unrelated filler document number {i}")
            kb.build_vector_index()
            results = kb.search("query words with no overlap")
        titles = [r["title"] for r in results]
        self.assertIn("target", titles)
        self.assertEqual(results[0]["title"], "target")  # best cosine wins

    def test_chunks_newer_than_sidecar_still_searchable(self):
        self._index("old", "unrelated old text")
        kb.build_vector_index()
        self._index("fresh", "brand new semantic match note")  # after build
        results = kb.search("query words")
        self.assertIn("fresh", [r["title"] for r in results])

    def test_deleted_chunk_since_build_is_skipped(self):
        self._index("gone", "semantic match text")
        self._index("kept", "another semantic match text")
        kb.build_vector_index()
        conn = kb.connect()
        conn.execute("DELETE FROM chunks WHERE id = (SELECT MIN(id) FROM chunks)")
        conn.commit()
        conn.close()
        results = kb.search("query words")
        self.assertEqual([r["title"] for r in results if r["title"] == "gone"], [])
        self.assertIn("kept", [r["title"] for r in results])

    def test_source_filtered_search_uses_pool_path(self):
        self._index("note", "semantic match research note")
        kb.build_vector_index()
        with mock.patch.object(kb, "_vector_full_scan") as full:
            kb.search("query words", sources="research")
        full.assert_not_called()

    def test_no_sidecar_falls_back_to_pool(self):
        self._index("only", "semantic match text")
        results = kb.search("query words")  # no build_vector_index()
        self.assertIn("only", [r["title"] for r in results])

    def test_rebuild_swaps_pointer_atomically(self):
        self._index("a", "keyword match one")
        m1 = kb.build_vector_index()
        self._index("b", "keyword match two")
        m2 = kb.build_vector_index()
        self.assertNotEqual(m1["dir"], m2["dir"])
        self.assertEqual(m2["count"], 2)
        ids, _, _ = kb._load_vector_index()
        self.assertEqual(len(ids), 2)


if __name__ == "__main__":
    unittest.main()
