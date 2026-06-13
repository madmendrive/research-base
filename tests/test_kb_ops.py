import json
import base64
import sqlite3
import tempfile
import time
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

from scripts import kb
from scripts.analyst import _filter_single_headline_context, _headline_web_freshness_query, _is_source_constrained_query
from scripts.claude_export import import_claude_export
from scripts.email_sweep import (
    _decode_gmail_raw,
    _ingest_saved_message,
    _is_low_value_email,
    _memory_scope_from_triage,
    email_sweep,
)
from scripts.heartbeat import _scheduled_slot_due, _time_due, load_agenda
from scripts.headlines import (
    _cnyes_row_to_item,
    _extract_article_text_from_html,
    _format_telegram_brief,
    _is_digest_candidate,
    _native_source_keys,
    _ngram_score,
    _parse_native_feed,
    _repair_text_encoding,
    _score_item,
)
from scripts.learning import add_lesson, init_schema as init_learning_schema, learning_context, log_interaction, record_feedback
from scripts.parallel_ingest import _destination_for, _normalize_triage
from scripts.research_memory import (
    DATA_DIR,
    _normalise_target_price_currency,
    _num,
    ingest_file as ingest_research_memory_file,
    init_schema as init_research_memory_schema,
)
from scripts.study import _cost_estimate_usd, _safe_slug, _source_file_mtime, _target_matches_filter
from scripts.tickers import canonicalize_ticker
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

    def test_fts_query_splits_market_suffix_punctuation(self):
        # Punctuation splits 3037.TT into 3037 + TT; tokens are quoted as FTS5
        # string literals so operator-like words can't break MATCH syntax.
        self.assertEqual(kb._fts_query("3037.TT ABF substrate"),
                         '"3037" OR "TT" OR "ABF" OR "substrate"')


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

    def test_telegram_brief_outputs_chinese_headlines_in_english(self):
        items = [
            {
                "rank": 12,
                "title": "SK海力士積極擴充HBM4產能 韓美半導體拿下設備訂單.",
                "description": "SK海力士積極擴充HBM4產能 韓美半導體拿下設備訂單 DIGITIMES.",
                "source": "DIGITIMES",
                "published_at": "2026-06-08T18:09:00+00:00",
                "url": "https://www.digitimes.com/example",
            }
        ]
        rows = [
            {
                "rank": 12,
                "key_sentence": "SK海力士積極擴充HBM4產能 韓美半導體拿下設備訂單",
                "summary": "SK海力士積極擴充HBM4產能 韓美半導體拿下設備訂單 DIGITIMES.",
            }
        ]
        brief = _format_telegram_brief(items, rows, window_hours=6)
        self.assertNotRegex(brief, r"[\u3400-\u9fff]")
        self.assertIn("SK Hynix is expanding HBM4 capacity", brief)
        self.assertIn("Hanmi Semiconductor", brief)

    def test_telegram_brief_translates_hbm_thermal_headline(self):
        items = [
            {
                "rank": 14,
                "title": "不再只拼層數 記憶體三大巨頭打響「HBM散熱戰」.",
                "description": "不再只拼層數 記憶體三大巨頭打響「HBM散熱戰」 news.cnyes.",
                "source": "news.cnyes.com",
                "published_at": "2026-06-08T23:37:00+00:00",
                "url": "https://www.cnyes.com/example",
            }
        ]
        rows = [
            {
                "rank": 14,
                "key_sentence": "不再只拼層數 記憶體三大巨頭打響「HBM散熱戰」",
                "summary": "不再只拼層數 記憶體三大巨頭打響「HBM散熱戰」 news.cnyes.",
            }
        ]
        brief = _format_telegram_brief(items, rows, window_hours=6)
        self.assertNotRegex(brief, r"[\u3400-\u9fff]")
        self.assertIn("thermal management", brief)
        self.assertIn("Samsung, SK Hynix, and Micron", brief)

    def test_telegram_brief_has_specific_chinese_cnyes_fallback(self):
        items = [
            {
                "rank": 7,
                "title": "SpaceX\u4e0a\u5e02\u6050\u885d\u64caAI\u80a1\u4f30\u503c\uff01\u7814\u7a76\u986f\u793a\u5927\u578bIPO\u4e00\u5e74\u5f8c\u5e73\u5747\u5831\u916c\u50c5\u52693.5%",
                "description": "",
                "source": "cnyes.com",
                "published_at": "2026-06-10T06:40:00+08:00",
                "url": "https://news.google.com/articles/example",
            }
        ]
        rows = [{"rank": 7, "key_sentence": items[0]["title"], "summary": items[0]["description"]}]
        brief = _format_telegram_brief(items, rows, window_hours=6)
        self.assertNotIn("appears to cover", brief)
        self.assertIn("SpaceX IPO could pressure AI-stock valuations", brief)
        self.assertIn("3.5% returns after one year", brief)

    def test_article_text_extractor_uses_meta_and_paragraphs(self):
        html = """
        <html><head>
          <meta property="og:title" content="ASML reaches a record market value">
          <meta name="description" content="JPMorgan and Goldman Sachs are bullish.">
        </head><body><article>
          <p>ASML rallied as investors focused on AI lithography demand and EUV bottlenecks.</p>
          <p>The report said leading-edge capacity remains constrained.</p>
        </article></body></html>
        """
        text = _extract_article_text_from_html(html)
        self.assertIn("ASML reaches a record market value", text)
        self.assertIn("AI lithography demand", text)

    def test_ngram_score_matches_marked_cnyes_title(self):
        a = "ASML\u5e02\u503c\u767b\u6b50\u6d32\u53f2\u4e0a\u65b0\u9ad8 \u6469\u6839\u5927\u901a\u3001\u9ad8\u76db\u9f4a\u558a\u8cb7"
        b = "<mark>ASML</mark>\u5e02\u503c\u767b\u6b50\u6d32\u53f2\u4e0a\u65b0\u9ad8 \u6469\u6839\u5927\u901a\u3001\u9ad8\u76db\u9f4a\u558a\u8cb7"
        self.assertGreaterEqual(_ngram_score(a, b), 0.95)

    def test_native_source_keys_map_approved_aliases(self):
        keys = _native_source_keys(["FT", "money.udn", "Commercial Times", "anue", "The Information"])
        self.assertIn("financial_times", keys)
        self.assertIn("udn", keys)
        self.assertIn("ctee", keys)
        self.assertIn("cnyes", keys)
        self.assertIn("the_information", keys)

    def test_parse_native_feed_returns_common_item_shape(self):
        feed = b"""<?xml version="1.0"?>
        <rss><channel><item>
          <title>ASML shares rise on AI lithography demand</title>
          <link>https://example.com/asml</link>
          <description>ASML demand is supported by AI chips.</description>
          <pubDate>Wed, 10 Jun 2026 01:00:00 GMT</pubDate>
        </item></channel></rss>"""
        items = _parse_native_feed(
            feed,
            feed_url="https://example.com/feed.xml",
            source="Example",
            window_hours=999999,
        )
        self.assertEqual(items[0]["source"], "Example")
        self.assertEqual(items[0]["url"], "https://example.com/asml")
        self.assertEqual(items[0]["discovery"], "native_rss")

    def test_repair_text_encoding_fixes_bloomberg_mojibake(self):
        text = "Chinaâ€™s chip sector â€“ and Nvidiaâ€™s suppliers"
        self.assertEqual(_repair_text_encoding(text), "China's chip sector - and Nvidia's suppliers")

    def test_repair_text_encoding_restores_chinese_mojibake(self):
        text = "ç¾Žå…‰è²¡å ±å‰ä¸‹è·Œèƒ½è²·å—Ž"
        self.assertEqual(_repair_text_encoding(text), "美光財報前下跌能買嗎")

    def test_digest_candidate_requires_hard_tech_signal(self):
        generic_video = {
            "title": "Paul Chan on Hong Kong's Market Outlook",
            "source": "Bloomberg",
            "url": "https://www.bloomberg.com/news/videos/2026-06-10/example",
            "description": "Hong Kong's financial secretary discusses capital flows.",
        }
        nvidia_market_item = {
            "title": "US Stocks Rebound From Selloff as Nvidia Leads Big-Tech Gains",
            "source": "Bloomberg",
            "url": "https://www.bloomberg.com/news/articles/2026-06-10/example",
            "description": "Nvidia and other AI chip names led the rebound.",
        }
        self.assertFalse(_is_digest_candidate(_score_item(generic_video)[1]))
        self.assertTrue(_is_digest_candidate(_score_item(nvidia_market_item)[1]))

    def test_cnyes_row_to_item_preserves_direct_url_and_article_text(self):
        item = _cnyes_row_to_item(
            {
                "newsId": 6491937,
                "title": "<mark>ASML</mark>\u5e02\u503c\u767b\u6b50\u6d32\u53f2\u4e0a\u65b0\u9ad8",
                "signature": "\u9245\u4ea8\u7db2\u65b0\u805e\u4e2d\u5fc3",
                "content": "AI \u9700\u6c42\u652f\u6490 EUV \u5149\u523b\u6a5f\u9700\u6c42\u3002",
                "publishAt": 1781041802,
            },
            category="tech",
        )
        self.assertEqual(item["url"], "https://news.cnyes.com/news/id/6491937")
        self.assertIn("ASML", item["title"])
        self.assertIn("EUV", item["article_text"])
        self.assertEqual(item["discovery"], "native_api")

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

    def test_headline_web_freshness_query_asks_for_prior_reporting(self):
        query = _headline_web_freshness_query(
            {
                "title": "Samsung is already shipping HBM4 for Vera Rubin",
                "source": "Digitimes",
                "published_at": "2026-06-09T00:00:00+00:00",
                "url": "https://example.com",
            }
        )
        self.assertIn("prior reports", query)
        self.assertIn("old/recycled news", query)
        self.assertIn("earliest reputable prior report", query)

    def test_single_headline_readthrough_includes_web_freshness_context(self):
        from scripts import analyst

        captured = {}

        def fake_call(prompt, max_tokens=4000):
            captured["prompt"] = prompt
            captured["max_tokens"] = max_tokens
            return "analysis"

        item = {
            "title": "Samsung is already shipping HBM4 for Vera Rubin",
            "source": "Digitimes",
            "published_at": "2026-06-09T00:00:00+00:00",
            "url": "https://example.com",
            "entities": {"tickers": ["005930 KS"], "themes": ["HBM"]},
        }
        with mock.patch.object(analyst.kb, "search", return_value=[]), \
             mock.patch("scripts.research_memory.query_context", return_value="structured memory"), \
             mock.patch("scripts.web_context.fetch_web_context", return_value="<live_web_context>Prior report found.</live_web_context>"), \
             mock.patch.object(analyst, "_call_claude", side_effect=fake_call):
            self.assertEqual(analyst.headline_readthrough([item]), "analysis")
        self.assertIn("Live web freshness / prior-reporting check", captured["prompt"])
        self.assertIn("Prior report found", captured["prompt"])
        self.assertIn("Freshness / novelty check", captured["prompt"])
        self.assertGreaterEqual(captured["max_tokens"], 5000)


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

    def test_research_memory_normalizes_target_price_currency_by_market(self):
        self.assertEqual(_normalise_target_price_currency("3583 TT", "USD"), "TWD")
        self.assertEqual(_normalise_target_price_currency("005930 KS", "USD"), "KRW")
        self.assertEqual(_normalise_target_price_currency("285A", "USD"), "JPY")
        self.assertEqual(_normalise_target_price_currency("2498.HK", "USD"), "HKD")
        self.assertEqual(_normalise_target_price_currency("AAPL", "USD"), "USD")

    def test_ticker_canonicalization_uses_company_aliases(self):
        self.assertEqual(canonicalize_ticker("005930.KS"), "005930 KS")
        self.assertEqual(canonicalize_ticker("3037.TW"), "3037 TT")
        self.assertEqual(canonicalize_ticker("285A.T"), "285A JT")
        self.assertEqual(canonicalize_ticker("6806.T"), "6806 JP")

    def test_research_memory_canonicalizes_source_subject(self):
        payload = {
            "metadata": {
                "source": "J.P. Morgan",
                "author": "JPM Analyst",
                "date": "2026-06-02",
                "title": "Samsung Memory Update",
                "source_type": "sellside_research",
            },
            "key_estimates": {"revenue": {"CY2026": "KRW 100tn"}},
        }
        test_dir = DATA_DIR / "005930.KS" / "research" / "notes" / "_test_research_memory"
        test_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_dir) as td:
            path = Path(td) / "JPM_Samsung_2026-06-02.pdf.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            ingest_research_memory_file(conn, path)
            source = conn.execute("SELECT subject FROM research_sources").fetchone()
            estimate = conn.execute("SELECT subject FROM research_estimates").fetchone()
            self.assertEqual(source["subject"], "005930 KS")
            self.assertEqual(estimate["subject"], "005930 KS")
            conn.close()

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
    def test_analyst_detects_source_constrained_query(self):
        self.assertTrue(
            _is_source_constrained_query(
                "What is the bull case for memory stocks, according to JPM's latest views?"
            )
        )

    def test_web_context_auto_triggers_for_latest_questions(self):
        self.assertTrue(
            should_use_web(
                "What is the latest news on Nvidia's current share price reaction?",
                kb_result_count=5,
                has_structured_context=True,
            )
        )

    def test_web_context_auto_skips_source_constrained_kb_questions(self):
        self.assertFalse(
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


class StudyAgentTests(unittest.TestCase):
    def test_safe_slug_removes_path_punctuation(self):
        self.assertEqual(_safe_slug("8035 JP / Tokyo Electron"), "8035_JP_Tokyo_Electron")

    def test_cost_estimate_uses_provider_defaults(self):
        cost = _cost_estimate_usd(1_000_000, 1_000_000, provider="anthropic", model="claude-opus-4-7")
        self.assertAlmostEqual(cost, 90.0)

    def test_source_file_mtime_uses_existing_extraction_files(self):
        test_dir = DATA_DIR / "MU" / "research" / "notes" / "_test_research_memory"
        test_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_dir) as td:
            path = Path(td) / "note.md.json"
            path.write_text("{}", encoding="utf-8")
            mtime = _source_file_mtime({"json_path": str(path), "source_path": str(path.with_suffix(""))})
        self.assertIsNotNone(mtime)
        self.assertLessEqual(mtime, time.time())

    def test_target_filter_matches_key_or_subject(self):
        self.assertTrue(_target_matches_filter("ticker:6723 JT", "6723 JT", "ticker", {"ticker:6723 jt"}))
        self.assertTrue(_target_matches_filter("ticker:6723 JT", "6723 JT", "ticker", {"6723 jt"}))
        self.assertFalse(_target_matches_filter("ticker:6723 JT", "6723 JT", "ticker", {"ticker:MU"}))


class EmailSweepTests(unittest.TestCase):
    def test_low_value_email_filter_skips_admin_mail(self):
        self.assertTrue(_is_low_value_email("Your payment receipt from Acid Investments #123"))
        self.assertTrue(_is_low_value_email("Welcome to SemiAnalysis"))
        self.assertTrue(_is_low_value_email("💬 New thread from Jason's Chips"))
        self.assertFalse(_is_low_value_email("Nvidia Earnings Review Q1 FY27"))

    def test_memory_scope_from_single_name_triage(self):
        scope = _memory_scope_from_triage({"primary_type": "single_name", "primary_subject": "3037.TW"})
        self.assertEqual(scope, {"corpus_type": "single_name", "subject_type": "ticker", "subject": "3037.TW"})

    def test_ingest_saved_message_parses_attached_eml(self):
        outer = EmailMessage()
        outer["Subject"] = "Wrapper email"
        outer["From"] = "sender@example.com"
        outer["Message-ID"] = "<outer@example.com>"
        outer.set_content("Please see attached research email.")

        inner = EmailMessage()
        inner["Subject"] = "NVDA HBM primer"
        inner["From"] = "substack@example.com"
        inner["Message-ID"] = "<inner@example.com>"
        inner.set_content("This is a deep dive on NVIDIA, HBM supply, and DRAM ASP assumptions.")
        outer.add_attachment(inner, filename="primer.eml")

        stats = {
            "indexed": 0,
            "attachments": 0,
            "queued_pdfs": 0,
            "eml_attachments": 0,
            "indexed_attachments": 0,
            "structured_extracted": 0,
            "structured_failed": 0,
        }
        test_dir = DATA_DIR / "MU" / "research" / "notes" / "_test_research_memory"
        test_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_dir) as td:
            with mock.patch("scripts.email_sweep.kb.index_text", return_value={"indexed": True}), \
                 mock.patch("scripts.email_sweep.enqueue_job"):
                _ingest_saved_message(
                    outer,
                    dest_dir=Path(td) / "email",
                    mailbox="INBOX",
                    uid="1",
                    analyse_attachments=False,
                    extract_research=False,
                    stats=stats,
                )
        self.assertEqual(stats["eml_attachments"], 1)
        self.assertEqual(stats["indexed"], 2)

    def test_ingest_saved_message_processes_attachments_without_wrapper_body(self):
        outer = EmailMessage()
        outer["Subject"] = "Research batch"
        outer["From"] = "sender@example.com"
        outer["Message-ID"] = "<outer-empty@example.com>"

        inner = EmailMessage()
        inner["Subject"] = "TSMC primer"
        inner["From"] = "substack@example.com"
        inner["Message-ID"] = "<inner-tsmc@example.com>"
        inner.set_content("This is a deep dive on TSMC, CoWoS, and AI accelerator demand.")
        outer.add_attachment(inner, filename="tsmc.eml")

        stats = {
            "indexed": 0,
            "attachments": 0,
            "queued_pdfs": 0,
            "eml_attachments": 0,
            "indexed_attachments": 0,
            "structured_extracted": 0,
            "structured_failed": 0,
        }
        test_dir = DATA_DIR / "MU" / "research" / "notes" / "_test_research_memory"
        test_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_dir) as td:
            with mock.patch("scripts.email_sweep.kb.index_text", return_value={"indexed": True}), \
                 mock.patch("scripts.email_sweep.enqueue_job"):
                _ingest_saved_message(
                    outer,
                    dest_dir=Path(td) / "email",
                    mailbox="INBOX",
                    uid="1",
                    analyse_attachments=False,
                    extract_research=False,
                    stats=stats,
                )
        self.assertEqual(stats["eml_attachments"], 1)
        self.assertEqual(stats["indexed"], 2)

    def test_decode_gmail_raw_accepts_base64url_without_padding(self):
        raw = base64.urlsafe_b64encode(b"Subject: Test\r\n\r\nBody").decode("ascii").rstrip("=")
        self.assertEqual(_decode_gmail_raw(raw), b"Subject: Test\r\n\r\nBody")

    def test_email_sweep_uses_gmail_api_backend(self):
        msg = EmailMessage()
        msg["Subject"] = "Substack deep dive"
        msg["From"] = "research@example.com"
        msg["Message-ID"] = "<gmail-test@example.com>"
        msg.set_content("This research discusses NVDA, HBM, and capex assumptions.")

        test_dir = DATA_DIR / "MU" / "research" / "notes" / "_test_research_memory"
        test_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_dir) as td:
            temp_email_dir = Path(td) / "email"
            with mock.patch.dict("os.environ", {"RESEARCH_EMAIL_PROVIDER": "gmail_api"}, clear=False), \
                 mock.patch("scripts.email_sweep.EMAIL_DIR", temp_email_dir), \
                 mock.patch("scripts.email_sweep.STATE_PATH", temp_email_dir / "_email_state.json"), \
                 mock.patch("scripts.email_sweep._iter_gmail_api_messages", return_value=("gmail:in:inbox", [("gmail:abc", msg.as_bytes())])), \
                 mock.patch("scripts.email_sweep.kb.index_text", return_value={"indexed": True}):
                stats = email_sweep(limit=10, extract_research=False)

        self.assertEqual(stats["provider"], "gmail_api")
        self.assertEqual(stats["seen"], 1)
        self.assertEqual(stats["new"], 1)
        self.assertEqual(stats["indexed"], 1)


if __name__ == "__main__":
    unittest.main()
