"""Tests for the agentic analyst path: tool execution and the tool-use loop.

All Anthropic calls are faked; no network. The jobs table lives in a temp
sqlite DB via a patched kb.connect.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import analyst, kb


class FakeBlock:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class FakeStream:
    def __init__(self, final):
        self._final = final
        self.text_stream = iter(
            b.text for b in final.content if b.type == "text"
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._final


class FakeFinal:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


class FakeMessages:
    def __init__(self, finals):
        self._finals = list(finals)
        self.calls = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self._finals.pop(0))


class FakeClient:
    def __init__(self, finals):
        self.messages = FakeMessages(finals)


class AnalystToolsBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "kb.sqlite"
        real_connect = kb.connect
        patcher = mock.patch.object(
            kb, "connect", lambda db_path=None: real_connect(self.db_path))
        patcher.start()
        self.addCleanup(patcher.stop)


class TestExecuteAnalystTool(AnalystToolsBase):
    def test_run_pipeline_job_enqueues_headline_sweep(self):
        out = analyst._execute_analyst_tool(
            "run_pipeline_job", {"kind": "headline_sweep", "window_hours": 11})
        self.assertIn("Queued headline_sweep as job #", out)
        conn = sqlite3.connect(self.db_path)
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT kind, status, payload_json FROM jobs").fetchone()
        self.assertEqual(row["kind"], "headline_sweep")
        self.assertEqual(row["status"], "queued")
        self.assertIn('"window_hours": 11', row["payload_json"])

    def test_run_pipeline_job_rejects_unknown_kind(self):
        with self.assertRaises(ValueError):
            analyst._execute_analyst_tool("run_pipeline_job", {"kind": "drop_tables"})

    def test_pipeline_status_lists_jobs(self):
        analyst._execute_analyst_tool("run_pipeline_job", {"kind": "folder_scan"})
        out = analyst._execute_analyst_tool("pipeline_status", {})
        self.assertIn("folder_scan", out)
        self.assertIn("queued", out)

    def test_unknown_tool_raises(self):
        with self.assertRaises(ValueError):
            analyst._execute_analyst_tool("rm_rf", {})


class TestAgenticLoop(AnalystToolsBase):
    def test_tool_use_then_final_answer(self):
        tool_call = FakeFinal(
            [FakeBlock("tool_use", id="tu_1", name="run_pipeline_job",
                       input={"kind": "headline_sweep", "window_hours": 11})],
            "tool_use",
        )
        final = FakeFinal([FakeBlock("text", text="Queued the catch-up Tech Brief.")],
                          "end_turn")
        client = FakeClient([tool_call, final])
        with mock.patch("scripts.llm_provider.get_client", return_value=client):
            answer = analyst._call_claude_agentic("run the 8am tech brief")
        self.assertEqual(answer, "Queued the catch-up Tech Brief.")

        # The job actually landed in the queue.
        conn = sqlite3.connect(self.db_path)
        self.addCleanup(conn.close)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='headline_sweep'").fetchone()[0],
            1)
        # Second API call carried the tool result back.
        second_call = client.messages.calls[1]
        roles = [m["role"] for m in second_call["messages"]]
        self.assertEqual(roles, ["user", "assistant", "user"])
        result_block = second_call["messages"][2]["content"][0]
        self.assertEqual(result_block["type"], "tool_result")
        self.assertEqual(result_block["tool_use_id"], "tu_1")
        self.assertIn("Queued headline_sweep", result_block["content"])
        # Tools were declared on both calls.
        self.assertTrue(all("tools" in c for c in client.messages.calls))

    def test_tool_error_is_returned_to_model(self):
        bad_call = FakeFinal(
            [FakeBlock("tool_use", id="tu_2", name="run_pipeline_job",
                       input={"kind": "bogus"})],
            "tool_use",
        )
        final = FakeFinal([FakeBlock("text", text="That job kind doesn't exist.")],
                          "end_turn")
        client = FakeClient([bad_call, final])
        with mock.patch("scripts.llm_provider.get_client", return_value=client):
            answer = analyst._call_claude_agentic("run the frobnicator")
        self.assertEqual(answer, "That job kind doesn't exist.")
        result_block = client.messages.calls[1]["messages"][2]["content"][0]
        self.assertTrue(result_block.get("is_error"))

    def test_openai_provider_falls_back_to_plain_path(self):
        with mock.patch.dict("os.environ", {"ANALYST_PROVIDER": "openai"}):
            with mock.patch.object(analyst, "_call_claude", return_value="plain") as plain:
                self.assertEqual(analyst._call_claude_agentic("q"), "plain")
        plain.assert_called_once()


if __name__ == "__main__":
    unittest.main()
