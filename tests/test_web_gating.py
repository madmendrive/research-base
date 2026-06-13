"""should_use_web gating — explicit web requests must win over KB context,
without false-firing on 'research'/'website'."""

import unittest
from unittest import mock

from scripts.web_context import should_use_web


class TestWebGating(unittest.TestCase):
    def _s(self, q, **kw):
        kw.setdefault("kb_result_count", 14)
        kw.setdefault("has_structured_context", True)
        with mock.patch.dict("os.environ", {"ANALYST_WEB_CONTEXT": "auto"}):
            return should_use_web(q, **kw)

    def test_explicit_web_wins_over_kb_context(self):
        # The reported bug: KB had hits, query said "live web search", web off.
        self.assertTrue(self._s(
            "Who are the main SPE vendors for SK Hynix and Samsung? "
            "Check both the KB and live web search"))

    def test_search_the_web_and_online(self):
        self.assertTrue(self._s("search the web for TSMC capex"))
        self.assertTrue(self._s("check online for the current Micron price"))

    def test_research_does_not_trigger_web(self):
        self.assertFalse(self._s("what does my research memory say about MU"))

    def test_website_does_not_trigger_web(self):
        self.assertFalse(self._s("whats on the company website"))

    def test_temporal_trigger_still_works(self):
        self.assertTrue(self._s("what is the latest on HBM pricing"))

    def test_plain_kb_question_stays_local(self):
        self.assertFalse(self._s("compare JPM and GS views on HBM"))

    def test_mode_off_overrides_explicit_web(self):
        with mock.patch.dict("os.environ", {"ANALYST_WEB_CONTEXT": "off"}):
            self.assertFalse(should_use_web("search the web for X",
                                            kb_result_count=14, has_structured_context=True))


if __name__ == "__main__":
    unittest.main()
