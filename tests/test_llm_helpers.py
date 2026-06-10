"""Characterization tests for the LLM helpers shared by research/macro/thematic.

Written against the behavior of the original per-module copies so the
consolidation into scripts.llm_provider / scripts.fileio keeps them green.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import macro, research, thematic


def _response(text="ok", stop_reason="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(text=text)], stop_reason=stop_reason)


def _transient_error():
    return RuntimeError("Connection error: server disconnected (529 overloaded)")


class FakeStream:
    def __init__(self, chunks, final):
        self._chunks = chunks
        self._final = final

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @property
    def text_stream(self):
        return iter(self._chunks)

    def get_final_message(self):
        return self._final


class FakeClient:
    """messages.create pops from create_outcomes; an Exception instance raises."""

    def __init__(self, create_outcomes, stream_outcomes=()):
        self.create_calls = []
        self.stream_calls = []
        self._create_outcomes = list(create_outcomes)
        self._stream_outcomes = list(stream_outcomes)
        self.messages = SimpleNamespace(create=self._create, stream=self._stream)

    def _create(self, **kwargs):
        self.create_calls.append(kwargs)
        outcome = self._create_outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def _stream(self, **kwargs):
        self.stream_calls.append(kwargs)
        outcome = self._stream_outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ParseJsonResponseTests(unittest.TestCase):
    MODULES = (research, macro, thematic)

    def test_plain_json(self):
        for module in self.MODULES:
            self.assertEqual(module._parse_json_response('{"a": 1}'), {"a": 1})

    def test_fenced_json(self):
        for module in self.MODULES:
            self.assertEqual(
                module._parse_json_response('```json\n{"a": 1}\n```'), {"a": 1}
            )

    def test_fenced_without_language_tag(self):
        for module in self.MODULES:
            self.assertEqual(module._parse_json_response('```\n{"a": 1}\n```'), {"a": 1})

    def test_invalid_json_raises_jsondecodeerror(self):
        # store_research/store_macro catch json.JSONDecodeError specifically.
        for module in self.MODULES:
            with self.assertRaises(json.JSONDecodeError):
                module._parse_json_response("not json at all")

    def test_non_object_json_allowed(self):
        for module in self.MODULES:
            self.assertEqual(module._parse_json_response("[1, 2]"), [1, 2])


class ExtractFileTextTests(unittest.TestCase):
    MODULES = (research, macro, thematic)

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def _write(self, name, data):
        path = self.dir / name
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")
        return path

    def test_reads_utf8_text(self):
        path = self._write("note.txt", "hello research")
        for module in self.MODULES:
            self.assertEqual(module._extract_file_text(path), "hello research")

    def test_reads_tsv(self):
        path = self._write("table.tsv", b"a\tb\n1\t2")
        for module in self.MODULES:
            self.assertEqual(module._extract_file_text(path), "a\tb\n1\t2")

    def test_latin1_fallback(self):
        path = self._write("legacy.txt", "caf\xe9".encode("latin-1"))
        for module in self.MODULES:
            self.assertEqual(module._extract_file_text(path), "caf\xe9")

    def test_truncates_to_max_chars(self):
        path = self._write("big.md", "x" * (research.MAX_TEXT_CHARS + 100))
        for module in self.MODULES:
            text = module._extract_file_text(path)
            self.assertTrue(text.endswith("[...truncated]"))
            self.assertLessEqual(
                len(text), module.MAX_TEXT_CHARS + len("\n[...truncated]")
            )

    def test_unsupported_suffix_raises(self):
        path = self._write("img.png", b"\x89PNG")
        for module in self.MODULES:
            with self.assertRaises(RuntimeError):
                module._extract_file_text(path)


class CallApiTests(unittest.TestCase):
    MODULES = (research, macro, thematic)

    def test_success_returns_text(self):
        for module in self.MODULES:
            client = FakeClient([_response("answer")])
            self.assertEqual(module._call_api(client, [{"role": "user", "content": "q"}]), "answer")

    def test_model_override_and_default(self):
        for module in self.MODULES:
            client = FakeClient([_response()])
            module._call_api(client, [], model="claude-sonnet-4-6")
            self.assertEqual(client.create_calls[0]["model"], "claude-sonnet-4-6")
            client = FakeClient([_response()])
            module._call_api(client, [])
            self.assertEqual(client.create_calls[0]["model"], module.RESEARCH_MODEL)

    def test_non_transient_error_raises_immediately(self):
        for module in self.MODULES:
            client = FakeClient([ValueError("bad request")])
            with self.assertRaises(ValueError):
                module._call_api(client, [])
            self.assertEqual(len(client.create_calls), 1)

    @mock.patch("time.sleep")
    def test_transient_error_falls_back_to_streaming(self, _sleep):
        client = FakeClient(
            [_transient_error()],
            [FakeStream(["par", "tial answer"], _response(stop_reason="end_turn"))],
        )
        result = research._call_api(client, [{"role": "user", "content": "q"}])
        self.assertEqual(result, "partial answer")
        self.assertEqual(len(client.stream_calls), 1)

    @mock.patch("time.sleep")
    def test_truncated_stream_retries(self, _sleep):
        client = FakeClient(
            [_transient_error(), _response("second try")],
            [FakeStream(["trunc"], _response(stop_reason="max_tokens"))],
        )
        result = research._call_api(client, [])
        self.assertEqual(result, "second try")

    def test_system_and_return_response_passthrough(self):
        system_blocks = [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]
        client = FakeClient([_response("with system")])
        resp = research._call_api(client, [], system=system_blocks, return_response=True)
        self.assertEqual(resp.content[0].text, "with system")
        self.assertEqual(client.create_calls[0]["system"], system_blocks)

    def test_no_system_key_when_absent(self):
        client = FakeClient([_response()])
        research._call_api(client, [])
        self.assertNotIn("system", client.create_calls[0])


if __name__ == "__main__":
    unittest.main()
