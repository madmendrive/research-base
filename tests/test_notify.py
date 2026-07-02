"""Telegram notify helper: delivery verification, retries, chunking."""

import unittest
from unittest import mock

from scripts import notify as N


def _resp(status=200, body=None, headers=None, text=""):
    r = mock.Mock()
    r.status_code = status
    r.headers = headers or {}
    r.text = text
    if body is None:
        body = {"ok": status == 200}
    r.json = mock.Mock(return_value=body)
    return r


ENV = {"TELEGRAM_BOT_TOKEN": "123:testtoken", "TELEGRAM_ALLOWED_USER_IDS": "42"}


class TestChunks(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(N.chunks("hello"), ["hello"])

    def test_no_empty_chunk_when_first_para_near_limit(self):
        # Regression: para of 3799 chars used to append an empty "" chunk first.
        text = "a" * 3799 + "\n\n" + "b"
        out = N.chunks(text)
        self.assertNotIn("", out)
        self.assertEqual("".join(out).count("a"), 3799)
        self.assertTrue(all(len(c) <= 3800 for c in out))

    def test_long_paragraph_split(self):
        out = N.chunks("x" * 8000)
        self.assertEqual(len(out), 3)
        self.assertNotIn("", out)


@mock.patch.dict("os.environ", ENV)
@mock.patch.object(N.time, "sleep", lambda *_: None)
class TestTelegramSend(unittest.TestCase):
    def test_success_returns_true(self):
        with mock.patch.object(N.requests, "post", return_value=_resp(200)) as post:
            self.assertTrue(N.telegram_send("hi"))
        post.assert_called_once()

    def test_429_retries_with_retry_after_then_succeeds(self):
        rate_limited = _resp(429, body={"ok": False, "parameters": {"retry_after": 3}})
        with mock.patch.object(
            N.requests, "post", side_effect=[rate_limited, _resp(200)]
        ) as post:
            self.assertTrue(N.telegram_send("hi"))
        self.assertEqual(post.call_count, 2)

    def test_400_fails_fast_and_returns_false(self):
        bad = _resp(400, body={"ok": False, "description": "can't parse entities"})
        with mock.patch.object(N.requests, "post", return_value=bad) as post, \
             self.assertLogs("notify", level="ERROR") as logs:
            self.assertFalse(N.telegram_send("hi"))
        post.assert_called_once()  # 4xx (non-429) must not retry
        self.assertIn("can't parse entities", "\n".join(logs.output))

    def test_5xx_retries_then_fails(self):
        with mock.patch.object(N.requests, "post", return_value=_resp(502)) as post, \
             self.assertLogs("notify", level="ERROR"):
            self.assertFalse(N.telegram_send("hi"))
        self.assertEqual(post.call_count, N.MAX_SEND_ATTEMPTS)

    def test_network_error_retries_then_fails(self):
        with mock.patch.object(
            N.requests, "post", side_effect=ConnectionError("boom")
        ) as post, self.assertLogs("notify", level="ERROR"):
            self.assertFalse(N.telegram_send("hi"))
        self.assertEqual(post.call_count, N.MAX_SEND_ATTEMPTS)

    def test_partial_chunk_failure_returns_false(self):
        text = "a" * 3799 + "\n\n" + "b" * 3799  # two chunks
        with mock.patch.object(
            N.requests, "post", side_effect=[_resp(200), _resp(400)]
        ), self.assertLogs("notify", level="ERROR"):
            self.assertFalse(N.telegram_send(text))

    def test_buttons_checks_response(self):
        bad = _resp(400, body={"ok": False, "description": "BUTTON_DATA_INVALID"})
        with mock.patch.object(N.requests, "post", return_value=bad), \
             self.assertLogs("notify", level="ERROR"):
            self.assertFalse(
                N.telegram_send_with_buttons("hi", [{"text": "b", "callback_data": "x"}])
            )

    def test_markdownish_html_propagates_failure(self):
        with mock.patch.object(N, "telegram_send", return_value=False):
            self.assertFalse(N.telegram_send_markdownish_html("**hi**"))


class TestMissingEnv(unittest.TestCase):
    def test_missing_token_returns_false(self):
        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_ALLOWED_USER_IDS": ""}), \
             mock.patch.object(N.requests, "post") as post, \
             self.assertLogs("notify", level="WARNING"):
            self.assertFalse(N.telegram_send("hi"))
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
