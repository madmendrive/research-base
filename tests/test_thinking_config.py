"""Adaptive-thinking + effort resolution and call_api plumbing."""

import unittest
from unittest import mock

from scripts import analyst
from scripts import llm_provider


class TestResolveThinking(unittest.TestCase):
    def test_default_is_adaptive_high(self):
        import os
        env = {k: v for k, v in os.environ.items()
               if k not in {"ANALYST_THINKING", "ANALYST_EFFORT",
                            "STUDY_THINKING", "STUDY_EFFORT"}}
        with mock.patch.dict("os.environ", env, clear=True):
            thinking, effort = analyst._resolve_thinking("ANALYST")
        self.assertEqual(thinking, {"type": "adaptive"})
        self.assertEqual(effort, "high")

    def test_off_disables(self):
        with mock.patch.dict("os.environ", {"ANALYST_THINKING": "off"}):
            self.assertEqual(analyst._resolve_thinking("ANALYST"), (None, None))

    def test_effort_override(self):
        with mock.patch.dict("os.environ", {"ANALYST_EFFORT": "max"}):
            _, effort = analyst._resolve_thinking("ANALYST")
        self.assertEqual(effort, "max")

    def test_invalid_effort_falls_back_to_high(self):
        with mock.patch.dict("os.environ", {"ANALYST_EFFORT": "ludicrous"}):
            _, effort = analyst._resolve_thinking("ANALYST")
        self.assertEqual(effort, "high")

    def test_study_prefix_independent_then_inherits(self):
        with mock.patch.dict("os.environ", {"STUDY_EFFORT": "medium", "ANALYST_EFFORT": "max"}):
            _, study_effort = analyst._resolve_thinking("STUDY")
            _, analyst_effort = analyst._resolve_thinking("ANALYST")
        self.assertEqual(study_effort, "medium")
        self.assertEqual(analyst_effort, "max")

    def test_study_inherits_analyst_when_unset(self):
        import os
        env = {k: v for k, v in os.environ.items()
               if k not in {"STUDY_EFFORT", "STUDY_THINKING"}}
        env["ANALYST_EFFORT"] = "xhigh"
        with mock.patch.dict("os.environ", env, clear=True):
            _, study_effort = analyst._resolve_thinking("STUDY")
        self.assertEqual(study_effort, "xhigh")


class TestCallApiThinkingKwargs(unittest.TestCase):
    def _fake_client(self):
        class Final:
            stop_reason = "end_turn"
            content = [type("B", (), {"type": "text", "text": "ok"})()]
        class Stream:
            text_stream = iter(["ok"])
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get_final_message(self): return Final()
        captured = {}
        class Msgs:
            def stream(self, **kw):
                captured.update(kw)
                return Stream()
        client = type("C", (), {"messages": Msgs()})()
        return client, captured

    def test_thinking_and_effort_passed_through(self):
        client, captured = self._fake_client()
        llm_provider.call_api(client, [{"role": "user", "content": "q"}],
                              thinking={"type": "adaptive"}, effort="high")
        self.assertEqual(captured["thinking"], {"type": "adaptive"})
        self.assertEqual(captured["output_config"], {"effort": "high"})

    def test_omitted_by_default(self):
        client, captured = self._fake_client()
        llm_provider.call_api(client, [{"role": "user", "content": "q"}])
        self.assertNotIn("thinking", captured)
        self.assertNotIn("output_config", captured)


if __name__ == "__main__":
    unittest.main()
