import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import kb
from scripts.analyst import _filter_single_headline_context
from scripts.claude_export import import_claude_export
from scripts.heartbeat import _scheduled_slot_due, _time_due, load_agenda
from scripts.headlines import _format_telegram_brief
from scripts.learning import add_lesson, init_schema as init_learning_schema, learning_context, log_interaction, record_feedback
from scripts.parallel_ingest import _destination_for, _normalize_triage
from scripts.research_memory import DATA_DIR, _num, ingest_file as ingest_research_memory_file, init_schema as init_research_memory_schema
from scripts.web_context import should_use_web


class KBTests(unittest.TestCase):
    def test_index_text_builds_chunks_and_fts(self):
        test_dir = DATA_DIR / "MU" / "research" / "notes" / "_test_research_memory"
        test_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_dir) as td:
            conn = kb.connect(Path(td) / "kb.sqlite")
            result = kb.index_text(
                title="NVDA HBM note",
                text="NVIDIA AI server demand is material for HBM, MU, and 000660 KS.",
                source_type="note",
                source_uri="test:note",
                metadata={"tickers": ["NVDA", "MU"]},
                embed=False,
                conn=conn,
            )
            self.assertTrue(result["indexed"])
            self.assertEqual(result["chunks"], 1)
            hits = conn.execute(
                "SELECT count(*) AS n FROM chunks_fts WHERE chunks_fts MATCH 'NVIDIA'"
            ).fetchone()["n"]
            self.assertEqual(hits, 1)
            conn.close()

    def test_chunk_text_overlaps_long_text(self):
        text = " ".join(f"word{i}" for i in range(1200))
        chunks = kb.chunk_text(text, chunk_chars=600, overlap=80)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunks))


class ImportTests(unittest.TestCase):
    def test_claude_export_dry_run_counts_conversation(self):
        payload = [
            {
                "uuid": "abc123",
                "name": "Semiconductor conversation",
                "chat_messages": [
                    {"sender": "human", "text": "What matters for HBM?"},
                    {"sender": "assistant", "text": "HBM matters for NVIDIA and memory suppliers."},
                ],
            }
        ]
        test_dir = DATA_DIR / "MU" / "research" / "notes" / "_test_research_memory"
        test_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_dir) as td:
            path = Path(td) / "conversations.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            stats = import_claude_export(path, dry_run=True)
            self.assertEqual(stats["conversations"], 1)
            self.assertEqual(stats["written"], 0)


class AgendaTests(unittest.TestCase):
    def test_agenda_front_matter_parser(self):
        text = """---
timezone: Asia/Hong_Kong
folder: /tmp/research
folder_sweep_times: [08:30, 20:30]
headline_interval_hours: 2
headline_sweep_times: [02:00, 08:00, 14:00, 20:00]
notify: true
---
"""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "agenda.md"
            path.write_text(text, encoding="utf-8")
            agenda = load_agenda(path)
            self.assertEqual(agenda["timezone"], "Asia/Hong_Kong")
            self.assertEqual(agenda["folder_sweep_times"], ["08:30", "20:30"])
            self.assertEqual(agenda["headline_sweep_times"], ["02:00", "08:00", "14:00", "20:00"])
            self.assertTrue(agenda["notify"])

    def test_headline_schedule_does_not_catch_up_old_slots(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        state = {}
        now = datetime(2026, 6, 8, 13, 45, tzinfo=ZoneInfo("Asia/Hong_Kong"))
        due = _time_due(
            now,
            ["02:00", "08:00", "14:00", "20:00"],
            state,
            "headline",
            catch_up=False,
        )
        self.assertFalse(due)

    def test_headline_schedule_exposes_slot_for_dedupe_key(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        state = {"headline:2026-06-08:14:00": "2026-06-08T14:00:56+08:00"}
        now = datetime(2026, 6, 8, 20, 0, 57, tzinfo=ZoneInfo("Asia/Hong_Kong"))
        due = _scheduled_slot_due(
            now,
            ["02:00", "08:00", "14:00", "20:00"],
            state,
            "headline",
            catch_up=False,
        )
        self.assertEqual(due, ("20:00", "headline:2026-06-08:20:00"))


class HeadlineBriefTests(unittest.TestCase):
    def test_telegram_brief_formats_inline_actions(self):
        items = [
            {
                "rank": 1,
                "source": "Digitimes",
                "published_at": "2026-06-08T03:02:00+00:00",
                "url": "https://www.digitimes.com/example",
            }
        ]
        rows = [
            {
                "rank": 1,
                "key_sentence": "Nvidia and MediaTek deepened AI chip cooperation",
                "summary": (
                    "MediaTek is expanding collaboration with Nvidia around AI chips. "
                    "The tie-up points to a broader Taiwan AI supply-chain push."
                ),
            }
        ]
        brief = _format_telegram_brief(items, rows, window_hours=6)
        self.assertIn("<b>Nvidia and MediaTek deepened AI chip cooperation.</b>", brief)
        self.assertIn("</b>\nMediaTek is expanding collaboration", brief)
        self.assertIn('<a href="https://www.digitimes.com/example">link</a>', brief)
        self.assertIn("analyse: /headline_1", brief)
        self.assertIn("/headline_1", brief)

    def test_telegram_brief_replaces_placeholder_summaries(self):
        items = [
            {
                "rank": 13,
                "title": "1Q26 Revenue Ranking among Top 10 Global Foundries.",
                "source": "TrendForce",
                "published_at": "2026-06-08T04:00:00+00:00",
                "url": "https://www.trendforce.com/example",
            },
            {
                "rank": 16,
                "title": "US Stocks Rebound From Selloff as Nvidia Leads Big-Tech Gains.",
                "source": "Bloomberg.com",
                "published_at": "2026-06-08T05:00:00+00:00",
                "url": "https://www.bloomberg.com/example",
            },
        ]
        rows = [
            {
                "rank": 13,
                "key_sentence": "1Q26 Revenue Ranking among Top 10 Global Foundries",
                "summary": "Source report from TrendForce; tap the analyse command for deeper read-through.",
            },
            {
                "rank": 16,
                "key_sentence": "US Stocks Rebound From Selloff as Nvidia Leads Big-Tech Gains",
                "summary": "Source report from Bloomberg.com; tap the analyse command for deeper read-through.",
            },
        ]
        brief = _format_telegram_brief(items, rows, window_hours=6)
        self.assertNotIn("Source report from", brief)
        self.assertNotIn("tap the analyse command", brief)
        self.assertIn("revenue rankings among global foundries", brief)
        self.assertIn("US equities led by Nvidia", brief)

    def test_single_headline_context_drops_digest_batches(self):
        context = [
            {
                "title": "Tech Brief 2026-06-08 13:42",
                "source_type": "headlines",
                "metadata": {"items": [{"rank": 1}, {"rank": 2}]},
            },
            {
                "title": "Unimicron ABF substrate note",
                "source_type": "research",
                "metadata": {},
            },
        ]
        filtered = _filter_single_headline_context(
            context,
            "Taiwan ecosystem strengthens AI chip supply chain",
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["title"], "Unimicron ABF substrate note")


class ResearchMemoryTests(unittest.TestCase):
    def test_research_memory_schema_initializes(self):
        conn = sqlite3.connect(":memory:")
        init_research_memory_schema(conn)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'research_%'"
            )
        }
        self.assertIn("research_sources", tables)
        self.assertIn("research_estimates", tables)
        conn.close()

    def test_research_memory_numeric_parser(self):
        self.assertEqual(_num("1,234.5%"), 1234.5)
        self.assertEqual(_num(">$1,000"), 1000.0)
        self.assertIsNone(_num("3x YoY increase"))

    def test_research_memory_ingests_nested_other_key_metrics(self):
        payload = {
            "metadata": {
                "source": "J.P. Morgan",
                "author": "JPM Analyst",
                "date": "2026-06-02",
                "title": "Memory Market Update",
                "source_type": "sellside_research",
            },
            "key_estimates": {
                "other_key_metrics": [
                    {
                        "metric": "DRAM ASP growth",
                        "values": {
                            "CY2026": {
                                "value": "+20%",
                                "unit": "%",
                                "source_detail": "JPM expects DRAM ASPs to rise on HBM tightness.",
                            }
                        },
                    }
                ]
            },
        }
        test_dir = DATA_DIR / "MU" / "research" / "notes" / "_test_research_memory"
        test_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_dir) as td:
            path = Path(td) / "JPM_Memory_Market_Update_2026-06-02.pdf.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            ingest_research_memory_file(conn, path)
            row = conn.execute(
                "SELECT publisher, author, metric, period, value_text, unit, source_detail FROM research_estimates"
            ).fetchone()
            self.assertEqual(row["publisher"], "J.P. Morgan")
            self.assertEqual(row["author"], "JPM Analyst")
            self.assertEqual(row["metric"], "DRAM ASP growth")
            self.assertEqual(row["period"], "CY2026")
            self.assertEqual(row["value_text"], "+20%")
            self.assertEqual(row["unit"], "%")
            self.assertIn("HBM tightness", row["source_detail"])
            conn.close()


class LearningMemoryTests(unittest.TestCase):
    def test_learning_schema_initializes(self):
        conn = sqlite3.connect(":memory:")
        init_learning_schema(conn)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'analyst_%'"
            )
        }
        self.assertIn("analyst_lessons", tables)
        self.assertIn("analyst_interactions", tables)
        self.assertIn("analyst_feedback", tables)
        conn.close()

    def test_learning_context_includes_lessons_and_feedback(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        add_lesson(
            "Always compare new substrate ideas against the best alternative expression.",
            tags=["process"],
            conn=conn,
        )
        interaction_id = log_interaction(
            "Should I buy 3037 TT?",
            "Use full sentences and compare alternatives.",
            user_id=123,
            conn=conn,
        )
        feedback_id = record_feedback(
            "You should distinguish explicit author trades from your own inferred trades.",
            user_id=123,
            conn=conn,
        )
        self.assertEqual(interaction_id, 1)
        self.assertEqual(feedback_id, 1)
        context = learning_context("3037 TT substrate alternative", conn=conn)
        self.assertIn("best alternative expression", context)
        self.assertIn("explicit author trades", context)
        conn.close()


class WebContextTests(unittest.TestCase):
    def test_web_context_auto_triggers_for_latest_questions(self):
        self.assertTrue(
            should_use_web(
                "What are JPM's latest DRAM ASP growth estimates for 2026 and 2027?",
                kb_result_count=5,
                has_structured_context=True,
            )
        )

    def test_web_context_auto_skips_static_kb_questions(self):
        self.assertFalse(
            should_use_web(
                "Compare SemiAnalysis and my notes on TSMC CoWoS bottlenecks.",
                kb_result_count=5,
                has_structured_context=True,
            )
        )


class FastIngestTests(unittest.TestCase):
    def test_fast_ingest_destination_for_single_name(self):
        triage = {
            "primary_type": "single_name",
            "primary_subject": "3037 TT",
            "confidence": "high",
        }
        dest, status = _destination_for(triage, Path("Unimicron.pdf"))
        self.assertEqual(status, "research")
        self.assertIn("3037 TT", str(dest))
        self.assertTrue(str(dest).endswith("_Unimicron.pdf"))

    def test_fast_ingest_normalizes_missing_triage_fields(self):
        triage = _normalize_triage({}, Path("note.pdf"))
        self.assertEqual(triage["primary_type"], "news_article")
        self.assertEqual(triage["primary_subject"], "News Article")
        self.assertEqual(triage["materiality"], {"tickers": {}, "themes": {}})


if __name__ == "__main__":
    unittest.main()
