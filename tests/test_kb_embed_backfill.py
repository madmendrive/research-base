"""KB embedding gap: error persistence on documents, backfill repair."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import kb


class _FailingEmbedder:
    model = "text-embedding-3-small"
    enabled = True

    def embed_texts(self, texts):
        raise RuntimeError("simulated embedding outage")


class _WorkingEmbedder:
    model = "text-embedding-3-small"
    enabled = True

    def embed_texts(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]


class TestEmbeddingErrorPersistence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "kb.sqlite"

    def _index_with_failure(self, conn):
        with mock.patch.object(kb, "EmbeddingClient", _FailingEmbedder):
            return kb.index_text(
                title="HBM note",
                text="HBM supply is tight through 2027 per the note.",
                source_type="note",
                source_uri="test:embfail",
                embed=True,
                conn=conn,
            )

    def test_embedding_failure_is_persisted_on_document(self):
        # Regression: the embedding_error marker was set on a local dict after
        # the documents row had already been written, so it never hit the DB
        # and 461k chunks sat unembedded invisibly.
        conn = kb.connect(self.db)
        result = self._index_with_failure(conn)
        self.assertTrue(result["indexed"])
        meta = json.loads(conn.execute(
            "SELECT metadata_json FROM documents WHERE id = ?",
            (result["document_id"],),
        ).fetchone()["metadata_json"])
        self.assertIn("embedding_error", meta)
        self.assertIn("simulated embedding outage", meta["embedding_error"])
        nulls = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE embedding_json IS NULL"
        ).fetchone()[0]
        self.assertGreater(nulls, 0)
        conn.close()

    def test_backfill_embeds_nulls_and_clears_marker(self):
        conn = kb.connect(self.db)
        result = self._index_with_failure(conn)
        doc_id = result["document_id"]
        conn.close()

        with mock.patch.object(kb, "EmbeddingClient", _WorkingEmbedder), \
             mock.patch.object(kb, "connect", lambda db_path=None: _reconnect(self.db)):
            stats = kb.embed_backfill()
        self.assertGreater(stats["embedded"], 0)
        self.assertEqual(stats["remaining"], 0)
        self.assertEqual(stats["documents_repaired"], 1)

        conn = kb.connect(self.db)
        nulls = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE embedding_json IS NULL"
        ).fetchone()[0]
        self.assertEqual(nulls, 0)
        meta = json.loads(conn.execute(
            "SELECT metadata_json FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()["metadata_json"])
        self.assertNotIn("embedding_error", meta)
        conn.close()

    def test_backfill_dry_run_reports_only(self):
        conn = kb.connect(self.db)
        self._index_with_failure(conn)
        conn.close()
        with mock.patch.object(kb, "connect", lambda db_path=None: _reconnect(self.db)):
            stats = kb.embed_backfill(dry_run=True)
        self.assertGreater(stats["unembedded"], 0)
        self.assertEqual(stats["embedded"], 0)

    def test_backfill_stops_cleanly_on_api_failure(self):
        conn = kb.connect(self.db)
        self._index_with_failure(conn)
        conn.close()
        with mock.patch.object(kb, "EmbeddingClient", _FailingEmbedder), \
             mock.patch.object(kb, "connect", lambda db_path=None: _reconnect(self.db)), \
             self.assertLogs("kb", level="ERROR"):
            stats = kb.embed_backfill()
        self.assertIn("error", stats)
        self.assertEqual(stats["embedded"], 0)

    def test_empty_chunk_text_padded_for_api(self):
        captured = {}

        class _Recorder:
            model = "m"
            enabled = True
            provider = "openai"

        client = kb.EmbeddingClient()
        fake = mock.Mock()
        fake.embeddings.create.side_effect = lambda **kw: captured.update(kw) or mock.Mock(
            data=[mock.Mock(embedding=[0.0]) for _ in kw["input"]]
        )
        client._client = fake
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "x"}):
            client.embed_texts(["", "real text"])
        self.assertEqual(captured["input"][0], " ")  # never an empty string


_REAL_CONNECT = kb.connect


def _reconnect(db_path):
    return _REAL_CONNECT(db_path)


if __name__ == "__main__":
    unittest.main()
