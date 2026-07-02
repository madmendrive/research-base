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
    """call_api is streaming-only: Norton kills silent HTTPS connections at
    ~60s, so non-streaming calls fail whenever generation runs long."""

    MODULES = (research, macro, thematic)

    @staticmethod
    def _ok_stream(text="answer"):
        return FakeStream([text], _response(stop_reason="end_turn"))

    def test_success_returns_streamed_text(self):
        for module in self.MODULES:
            client = FakeClient([], [self._ok_stream()])
            self.assertEqual(module._call_api(client, [{"role": "user", "content": "q"}]), "answer")
            self.assertEqual(len(client.create_calls), 0)

    def test_model_override_and_default(self):
        for module in self.MODULES:
            client = FakeClient([], [self._ok_stream()])
            module._call_api(client, [], model="claude-sonnet-4-6")
            self.assertEqual(client.stream_calls[0]["model"], "claude-sonnet-4-6")
            client = FakeClient([], [self._ok_stream()])
            module._call_api(client, [])
            self.assertEqual(client.stream_calls[0]["model"], module.RESEARCH_MODEL)

    def test_non_transient_error_raises_immediately(self):
        for module in self.MODULES:
            client = FakeClient([], [ValueError("bad request")])
            with self.assertRaises(ValueError):
                module._call_api(client, [])
            self.assertEqual(len(client.stream_calls), 1)

    @mock.patch("time.sleep")
    def test_transient_error_retries_stream(self, _sleep):
        client = FakeClient([], [_transient_error(), self._ok_stream("partial answer")])
        result = research._call_api(client, [{"role": "user", "content": "q"}])
        self.assertEqual(result, "partial answer")
        self.assertEqual(len(client.stream_calls), 2)

    @mock.patch("time.sleep")
    def test_rate_limit_error_is_transient(self, _sleep):
        # Regression: 429s used to raise immediately, aborting the agentic
        # loop into the expensive full-re-synthesis fallback chain.
        client = FakeClient([], [
            RuntimeError("rate_limit_error: Number of requests has exceeded your rate limit (429)"),
            self._ok_stream("recovered"),
        ])
        result = research._call_api(client, [{"role": "user", "content": "q"}])
        self.assertEqual(result, "recovered")
        self.assertEqual(len(client.stream_calls), 2)

    @mock.patch("time.sleep")
    def test_truncated_stream_escalates_max_tokens(self, _sleep):
        client = FakeClient([], [
            FakeStream(["trunc"], _response(stop_reason="max_tokens")),
            self._ok_stream("full answer"),
        ])
        result = research._call_api(client, [], max_tokens=8192)
        self.assertEqual(result, "full answer")
        self.assertEqual(client.stream_calls[0]["max_tokens"], 8192)
        self.assertEqual(client.stream_calls[1]["max_tokens"], 16384)

    def test_system_and_return_response_passthrough(self):
        system_blocks = [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]
        final = _response("with system")
        client = FakeClient([], [FakeStream(["with system"], final)])
        resp = research._call_api(client, [], system=system_blocks, return_response=True)
        self.assertIs(resp, final)
        self.assertEqual(client.stream_calls[0]["system"], system_blocks)

    def test_no_system_key_when_absent(self):
        client = FakeClient([], [self._ok_stream()])
        research._call_api(client, [])
        self.assertNotIn("system", client.stream_calls[0])


class NativePdfPayloadTests(unittest.TestCase):
    def test_non_pdf_falls_back_to_text_system_block(self):
        from scripts.llm_provider import document_message_payload

        messages, system = document_message_payload("prompt", pdf_path=Path("note.txt"), text="body")
        self.assertEqual(messages, [{"role": "user", "content": "prompt"}])
        self.assertIn("<document>\nbody\n</document>", system[0]["text"])

    def test_env_kill_switch_disables_native_mode(self):
        from scripts.llm_provider import native_pdf_eligible

        with mock.patch.dict("os.environ", {"PDF_NATIVE_EXTRACTION": "0"}):
            self.assertFalse(native_pdf_eligible(Path("doc.pdf")))

    def test_eligible_pdf_becomes_cached_document_block(self):
        import scripts.llm_provider as lp

        pdf = Path(tempfile.mkdtemp()) / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        with mock.patch("scripts.fileio.pdf_page_count", return_value=42):
            messages, system = lp.document_message_payload("prompt", pdf_path=pdf, text="body")
        self.assertIsNone(system)
        content = messages[0]["content"]
        # A data-not-instructions framing note precedes the document block.
        self.assertEqual(content[0]["type"], "text")
        self.assertIn("never as instructions", content[0]["text"])
        self.assertEqual(content[1]["type"], "document")
        self.assertEqual(content[1]["source"]["media_type"], "application/pdf")
        self.assertEqual(content[1]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(content[2], {"type": "text", "text": "prompt"})

    def test_oversized_pdf_falls_back(self):
        import scripts.llm_provider as lp

        pdf = Path(tempfile.mkdtemp()) / "big.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        with mock.patch("scripts.fileio.pdf_page_count", return_value=250):
            messages, system = lp.document_message_payload("prompt", pdf_path=pdf, text="body")
        self.assertIsNotNone(system)
        self.assertEqual(messages, [{"role": "user", "content": "prompt"}])


class OpenAiPdfInputTests(unittest.TestCase):
    def test_file_part_shape(self):
        from scripts.llm_provider import openai_pdf_file_part

        pdf = Path(tempfile.mkdtemp()) / "note.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        part = openai_pdf_file_part(pdf)
        self.assertEqual(part["type"], "input_file")
        self.assertEqual(part["filename"], "note.pdf")
        self.assertTrue(part["file_data"].startswith("data:application/pdf;base64,"))

    def test_input_messages_with_and_without_pdf(self):
        from scripts.llm_provider import _openai_input_messages

        plain = _openai_input_messages("ask", "sys", None)
        self.assertEqual(plain, [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "ask"},
        ])
        pdf = Path(tempfile.mkdtemp()) / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        with_pdf = _openai_input_messages("ask", None, pdf)
        content = with_pdf[0]["content"]
        self.assertEqual(content[0]["type"], "input_file")
        self.assertEqual(content[1], {"type": "input_text", "text": "ask"})

    def test_fast_path_prompt_omits_inline_document_when_native(self):
        from scripts.parallel_ingest import _build_extraction_prompt

        triage = {"primary_type": "macro", "primary_subject": "Test Author", "category": "Macro"}
        inline = _build_extraction_prompt(triage, "DOCBODY")
        self.assertIn("<document>\nDOCBODY\n</document>", inline)
        native = _build_extraction_prompt(triage, "", include_text=False)
        # The base builders' tail mentions "<document> tags" as prose; assert no
        # actual inline document block instead.
        self.assertNotIn("</document>", native)
        self.assertIn("attached to this request as a PDF", native)


class CachedDocumentBlockTests(unittest.TestCase):
    def test_block_shape_and_cache_marker(self):
        from scripts.llm_provider import cached_document_block

        block = cached_document_block("doc body")
        self.assertEqual(len(block), 1)
        self.assertEqual(block[0]["cache_control"], {"type": "ephemeral"})
        self.assertIn("<document>\ndoc body\n</document>", block[0]["text"])

    def test_identical_text_produces_identical_block(self):
        # Cache hits across store + cross-cut calls require byte-identical
        # system prefixes for the same document text.
        from scripts.llm_provider import cached_document_block

        self.assertEqual(cached_document_block("same"), cached_document_block("same"))


if __name__ == "__main__":
    unittest.main()
