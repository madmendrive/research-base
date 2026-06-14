"""Web context: retry transient errors, then fall back to the other provider."""

import unittest
from unittest import mock

from scripts import web_context as W


class RateLimit(Exception):
    pass


class TestWebFallback(unittest.TestCase):
    def setUp(self):
        # Force the gate on so fetch_web_context always attempts.
        p = mock.patch.dict("os.environ", {
            "ANALYST_WEB_CONTEXT": "always", "ANALYST_WEB_PROVIDER": "openai",
            "ANALYST_WEB_FALLBACK": "1", "ANALYST_WEB_ATTEMPTS": "2",
            "ANALYST_WEB_FAIL_CLOSED": "0",
        })
        p.start(); self.addCleanup(p.stop)
        s = mock.patch("time.sleep")  # no real backoff delay
        s.start(); self.addCleanup(s.stop)

    def test_openai_ratelimit_falls_back_to_anthropic(self):
        with mock.patch.object(W, "_call_openai_web", side_effect=RateLimit("rate limit upstream (429)")), \
             mock.patch.object(W, "_call_anthropic_web", return_value="anthropic web result"):
            out = W.fetch_web_context("q", kb_result_count=5, has_structured_context=True)
        self.assertIn("anthropic web result", out)
        self.assertIn("<live_web_context>", out)

    def test_transient_retried_then_succeeds_same_provider(self):
        calls = {"n": 0}
        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RateLimit("429 rate limit")
            return "openai second-try result"
        with mock.patch.object(W, "_call_openai_web", side_effect=flaky), \
             mock.patch.object(W, "_call_anthropic_web", return_value="should not be used"):
            out = W.fetch_web_context("q", kb_result_count=5, has_structured_context=True)
        self.assertIn("openai second-try result", out)
        self.assertEqual(calls["n"], 2)

    def test_both_providers_fail_returns_failure_note(self):
        with mock.patch.object(W, "_call_openai_web", side_effect=RateLimit("429")), \
             mock.patch.object(W, "_call_anthropic_web", side_effect=Exception("anthropic down")):
            out = W.fetch_web_context("q", kb_result_count=5, has_structured_context=True)
        self.assertIn("failed", out.lower())
        self.assertIn("openai", out)
        self.assertIn("anthropic", out)

    def test_non_transient_skips_retry_goes_to_fallback(self):
        calls = {"openai": 0}
        def bad_request(*a, **k):
            calls["openai"] += 1
            raise ValueError("400 invalid request")  # non-transient
        with mock.patch.object(W, "_call_openai_web", side_effect=bad_request), \
             mock.patch.object(W, "_call_anthropic_web", return_value="fallback ok"):
            out = W.fetch_web_context("q", kb_result_count=5, has_structured_context=True)
        self.assertIn("fallback ok", out)
        self.assertEqual(calls["openai"], 1)  # not retried


if __name__ == "__main__":
    unittest.main()
