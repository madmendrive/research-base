"""Document classifier — uses Claude API to categorize downloaded documents."""

import json
import os
import warnings
from datetime import datetime, timezone
from pathlib import Path

import pdfplumber
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from anthropic import Anthropic

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

CLASSIFY_MODEL = "claude-haiku-4-5-20251001"  # IR-doc routing (10-K vs earnings) — Haiku is plenty, ~3x cheaper than Sonnet 4.

CLASSIFICATION_PROMPT = """\
You are classifying an investor relations document. Based on the text below \
from the first pages, respond with ONLY a JSON object (no markdown, no explanation):
{
  "document_type": one of ["earnings_release", "quarterly_presentation", \
"annual_report", "annual_presentation", "investor_day", "press_release", \
"financial_supplement", "proxy_statement", "regulatory_filing", "other"],
  "period": "Q1 2024" or "FY2023" or "H1 2024" or null if unclear,
  "date": "YYYY-MM-DD" or null if unclear,
  "company_name": "...",
  "title": "brief title describing the document"
}

Document text:
"""

CLASSIFIABLE_EXTENSIONS = {".pdf", ".htm", ".html"}


def extract_text(path, max_pages=2, max_chars=None):
    """Extract text from a PDF or HTML file.
    For PDFs, extracts first max_pages pages.
    For HTML, extracts full visible text.
    If max_chars is set, truncates the result."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        text, err = _extract_pdf_text(path, max_pages=max_pages)
    elif suffix in (".htm", ".html"):
        text, err = _extract_html_text(path)
    else:
        return None, f"unsupported file type: {suffix}"

    if text and max_chars and len(text) > max_chars:
        text = text[:max_chars] + "\n[...truncated]"
    return text, err


def _extract_pdf_text(path, max_pages=2):
    """Extract text from the first N pages of a PDF."""
    text_parts = []
    try:
        with pdfplumber.open(path) as pdf:
            for i, page in enumerate(pdf.pages):
                if i >= max_pages:
                    break
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
    except Exception as e:
        return None, str(e)
    return "\n\n".join(text_parts), None


def _extract_html_text(path):
    """Extract visible text from an HTML file."""
    try:
        raw = path.read_bytes()
        # Try UTF-8 first, fall back to latin-1
        try:
            html = raw.decode("utf-8")
        except UnicodeDecodeError:
            html = raw.decode("latin-1")
        soup = BeautifulSoup(html, "lxml")
        # Remove script/style elements
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        if not text:
            return None, "empty document"
        return text, None
    except Exception as e:
        return None, str(e)


class _ApiFatalError(Exception):
    """Raised when the API returns an error that won't resolve by retrying."""
    pass


def _classify_text(client, text):
    """Send text to Claude for classification. Returns parsed dict or None.
    Raises _ApiFatalError for billing/auth errors that affect all requests."""
    prompt = CLASSIFICATION_PROMPT + text

    try:
        response = client.messages.create(
            model=CLASSIFY_MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        err_str = str(e)
        if any(kw in err_str for kw in [
            "credit balance is too low",
            "invalid x-api-key",
            "invalid api key",
            "authentication_error",
        ]):
            raise _ApiFatalError(err_str)
        print(f"    API error: {e}")
        return None

    raw = response.content[0].text.strip()

    # Strip markdown fences if the model wraps the JSON
    if raw.startswith("```"):
        lines = raw.splitlines()
        lines = [l for l in lines if not l.startswith("```")]
        raw = "\n".join(lines)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"    Could not parse API response as JSON: {raw[:120]}...")
        return None


def _load_classifications(path):
    """Load existing classifications.json, or return empty structure."""
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"files": [], "classified_at": None}


def _save_classifications(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _collect_files(ticker_dir):
    """Collect all classifiable files across ir/ and sec/ subdirectories.
    Skips the filings/ folder (organized copies) and financials/ folder."""
    files = []
    skip_names = {"download_log.json", "classifications.json", "extracted_data.json"}
    skip_dirs = {"filings", "financials"}

    for sub in sorted(ticker_dir.iterdir()):
        if not sub.is_dir():
            continue
        if sub.name in skip_dirs:
            continue
        # Walk into subdirectories (e.g. sec/10-K/, sec/10-Q/, ir/)
        for f in sorted(sub.rglob("*")):
            if not f.is_file():
                continue
            if f.name in skip_names:
                continue
            if f.suffix.lower() in CLASSIFIABLE_EXTENSIONS:
                files.append(f)

    return files


def classify_documents(ticker, reclassify=False):
    """Classify all documents in data/{ticker}/ using the Claude API.

    Args:
        ticker: Company ticker.
        reclassify: If True, re-classify files even if they already have entries.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY environment variable is not set.")
        print("  Set it with: export ANTHROPIC_API_KEY=sk-ant-...")
        return

    ticker_dir = DATA_DIR / ticker
    if not ticker_dir.exists():
        print(f"Error: no data found for '{ticker}' at {ticker_dir}")
        print("  Run: python main.py download-materials " + ticker)
        return

    # Find all classifiable files across all subdirectories
    all_files = _collect_files(ticker_dir)

    if not all_files:
        print(f"No classifiable files found in {ticker_dir}")
        return

    # Load existing classifications (stored at ticker level)
    classifications_path = ticker_dir / "classifications.json"
    existing = _load_classifications(classifications_path)
    already_classified = set()
    if not reclassify:
        already_classified = {entry["filename"] for entry in existing["files"]}

    # Determine which files need classification
    to_classify = [f for f in all_files if f.name not in already_classified]

    print(f"=== Classifier: {ticker} ===")
    print(f"Files found: {len(all_files)}")
    print(f"Already classified: {len(already_classified)}")
    print(f"To classify: {len(to_classify)}")

    if not to_classify:
        print("Nothing to classify.")
        _print_summary(existing)
        return

    client = Anthropic(api_key=api_key)

    # If reclassifying, start fresh
    if reclassify:
        entries = []
    else:
        entries = list(existing["files"])

    classified_count = 0
    failed_count = 0

    for file_path in to_classify:
        safe_name = file_path.name.encode("ascii", errors="replace").decode()
        rel_path = str(file_path.relative_to(ticker_dir)).encode("ascii", errors="replace").decode()
        print(f"\n  Classifying: {rel_path}")

        # Extract text (first 2 pages for PDF, full for HTML, truncated to 4000 chars)
        text, err = extract_text(file_path, max_pages=2, max_chars=4000)
        if not text:
            print(f"    Could not extract text: {err or 'empty document'}")
            entries.append({
                "filename": file_path.name,
                "path": str(file_path.relative_to(PROJECT_ROOT)),
                "source_dir": file_path.parent.relative_to(ticker_dir).as_posix(),
                "document_type": "unclassified",
                "period": None,
                "date": None,
                "company_name": None,
                "title": None,
                "error": err or "empty document",
            })
            failed_count += 1
            continue

        # Classify
        try:
            result = _classify_text(client, text)
        except _ApiFatalError as e:
            print(f"\n  Fatal API error — stopping classification.")
            print(f"  {e}")
            break
        if result is None:
            entries.append({
                "filename": file_path.name,
                "path": str(file_path.relative_to(PROJECT_ROOT)),
                "source_dir": file_path.parent.relative_to(ticker_dir).as_posix(),
                "document_type": "unclassified",
                "period": None,
                "date": None,
                "company_name": None,
                "title": None,
            })
            failed_count += 1
        else:
            entries.append({
                "filename": file_path.name,
                "path": str(file_path.relative_to(PROJECT_ROOT)),
                "source_dir": file_path.parent.relative_to(ticker_dir).as_posix(),
                "document_type": result.get("document_type", "other"),
                "period": result.get("period"),
                "date": result.get("date"),
                "company_name": result.get("company_name"),
                "title": result.get("title"),
            })
            doc_type = result.get("document_type", "?")
            period = result.get("period") or "—"
            print(f"    -> {doc_type} | {period}")
            classified_count += 1

    # Save results
    output = {
        "files": entries,
        "classified_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_classifications(classifications_path, output)

    print(f"\n=== Classification complete ===")
    print(f"Classified: {classified_count}")
    if failed_count:
        print(f"Failed/unclassified: {failed_count}")
    print(f"Saved to: {classifications_path}")

    _print_summary(output)


def _print_summary(data):
    """Print a summary table of classified files."""
    files = data.get("files", [])
    if not files:
        return

    print(f"\n{'Filename':<55} {'Type':<25} {'Period':<12}")
    print("-" * 92)
    for entry in files:
        name = entry["filename"]
        if len(name) > 52:
            name = name[:49] + "..."
        name = name.encode("ascii", errors="replace").decode()
        doc_type = entry.get("document_type", "?")
        period = entry.get("period") or "—"
        print(f"{name:<55} {doc_type:<25} {period:<12}")
