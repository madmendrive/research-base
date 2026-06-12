"""Image-only PDFs (no text layer) must triage via native PDF attachment."""

import unittest
from pathlib import Path
from unittest import mock

from scripts import parallel_ingest


class TestImageOnlyTriage(unittest.TestCase):
    def test_empty_text_falls_back_to_native_pdf(self):
        captured = {}

        def fake_complete_json(prompt, *, config, system=None, max_output_tokens=0, pdf_path=None):
            captured["pdf_path"] = pdf_path
            captured["prompt"] = prompt
            return {"primary_type": "single_name", "primary_subject": "MU",
                    "title": "BofA note", "confidence": "high"}

        with mock.patch.object(parallel_ingest, "extract_text", return_value=("", "empty")), \
             mock.patch("scripts.llm_provider.native_pdf_eligible", return_value=True), \
             mock.patch.object(parallel_ingest, "complete_json", side_effect=fake_complete_json):
            result = parallel_ingest._triage_with_provider(Path("Bofa_note.pdf"))

        self.assertEqual(captured["pdf_path"], Path("Bofa_note.pdf"))
        self.assertIn("no machine-readable text layer", captured["prompt"])
        self.assertEqual(result["primary_subject"], "MU")

    def test_empty_text_and_ineligible_pdf_still_raises(self):
        with mock.patch.object(parallel_ingest, "extract_text", return_value=("", "empty")), \
             mock.patch("scripts.llm_provider.native_pdf_eligible", return_value=False):
            with self.assertRaises(RuntimeError):
                parallel_ingest._triage_with_provider(Path("huge_scan.pdf"))

    def test_normal_text_path_does_not_attach_pdf(self):
        captured = {}

        def fake_complete_json(prompt, *, config, system=None, max_output_tokens=0, pdf_path=None):
            captured["pdf_path"] = pdf_path
            return {"primary_type": "macro", "primary_subject": "Some Author"}

        with mock.patch.object(parallel_ingest, "extract_text",
                               return_value=("plenty of extracted text", None)), \
             mock.patch.object(parallel_ingest, "complete_json", side_effect=fake_complete_json):
            parallel_ingest._triage_with_provider(Path("normal.pdf"))
        self.assertIsNone(captured["pdf_path"])


if __name__ == "__main__":
    unittest.main()
