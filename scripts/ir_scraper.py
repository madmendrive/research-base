"""IR website downloader — scrapes investor relations pages for documents."""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "companies.json"
DATA_DIR = PROJECT_ROOT / "data"

USER_AGENT = "ResearchPipeline/1.0 (ernest.limwj@gmail.com)"

DOWNLOAD_DELAY = 1  # seconds between downloads
DEFAULT_LIMIT = 200  # max files per run
DEFAULT_SINCE_YEAR = 2020  # only download documents from this year onward

# File extensions we want to download
DOWNLOADABLE_EXTENSIONS = {
    ".pdf", ".pptx", ".ppt", ".xlsx", ".xls", ".doc", ".docx",
}

# Keywords that identify relevant IR sub-section links
SECTION_KEYWORDS_EN = [
    "financial results", "earnings", "quarterly results", "annual results",
    "press releases", "press release", "news",
    "presentations", "events", "investor day",
    "annual report", "annual review", "integrated report",
    "sec filings", "regulatory filings", "filings",
    "reports", "publications",
]
SECTION_KEYWORDS_JA = [
    "決算", "ir資料", "有価証券報告書", "決算短信", "説明会",
    "業績", "財務情報", "株主総会", "アニュアルレポート",
]
SECTION_KEYWORDS_KO = [
    "실적", "공시", "ir자료", "재무정보", "사업보고서",
]
SECTION_KEYWORDS_ZH = [
    "財務報告", "法人說明會", "年報", "財務資訊", "投資人關係",
]

ALL_SECTION_KEYWORDS = (
    SECTION_KEYWORDS_EN + SECTION_KEYWORDS_JA +
    SECTION_KEYWORDS_KO + SECTION_KEYWORDS_ZH
)


def _load_companies():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _get_session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def _is_downloadable(url):
    """Check if a URL points to a downloadable file."""
    parsed = urlparse(url)
    path = unquote(parsed.path).lower()
    query = unquote(parsed.query).lower()
    full = (path + "?" + query).lower()
    # Standard file extension check
    if any(path.endswith(ext) for ext in DOWNLOADABLE_EXTENSIONS):
        return True
    # API-style download URLs (common on TW/Asian corporate sites)
    download_patterns = ["download", "get_ir", "getfile", "filedownload", "attachment"]
    if any(p in full for p in download_patterns):
        return True
    return False


def _extract_filename(url):
    """Extract a filename from a URL, falling back to a sanitised path."""
    parsed = urlparse(url)
    path = unquote(parsed.path)
    query = unquote(parsed.query)
    name = Path(path).name
    if not name or "." not in name:
        # Build a name from path + query for API-style URLs
        full = path.strip("/") + ("_" + query if query else "")
        name = re.sub(r"[^\w.\-]", "_", full) or "unknown"
        # Add .pdf extension if not present
        if not any(name.endswith(ext) for ext in DOWNLOADABLE_EXTENSIONS):
            name += ".pdf"
    return name


def _is_same_domain(file_url, ir_url):
    """Check if a file URL is on the same domain (or subdomain) as the IR URL."""
    ir_domain = urlparse(ir_url).netloc.lower()
    file_domain = urlparse(file_url).netloc.lower()

    # Extract the base domain (last two parts, e.g. "apple.com")
    def base_domain(netloc):
        parts = netloc.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else netloc

    return base_domain(file_domain) == base_domain(ir_domain)


def _extract_year_from_url(url):
    """Try to extract a year from a URL path or filename.
    Recognises 4-digit years (2000–2099) and 2-digit fiscal year refs like FY23.
    Returns the year as an int, or None if not found."""
    path = unquote(urlparse(url).path)
    years = set()

    # 4-digit years: 2000–2099
    for m in re.findall(r"20[0-9]{2}", path):
        years.add(int(m))

    # 2-digit fiscal year references: FY23, fy2024 (already caught above),
    # FY23, fy-23, etc.
    for m in re.findall(r"[Ff][Yy][-_]?([0-9]{2})(?![0-9])", path):
        y = int(m)
        years.add(2000 + y)

    if not years:
        return None
    # Return the largest (most recent) year found
    return max(years)


def _is_since_year(url, since_year):
    """Check if a URL's detected year is >= since_year.
    If no year is detectable, the file is kept (benefit of the doubt)."""
    if since_year <= 0:
        return True
    year = _extract_year_from_url(url)
    if year is None:
        return True  # can't tell — keep it
    return year >= since_year


def _looks_like_js_rendered(html):
    """Heuristic: page has very little visible text relative to its size."""
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(separator=" ", strip=True)
    # If the HTML is large but text content is tiny, it's probably JS-rendered
    if len(html) > 2000 and len(text) < 200:
        return True
    # Common SPA markers
    if soup.find("div", id="root") and len(text) < 300:
        return True
    if soup.find("div", id="app") and len(text) < 300:
        return True
    return False


def _fetch_page_requests(url, session):
    """Fetch a page with requests. Returns (html, final_url) or (None, None)."""
    try:
        resp = session.get(url, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        return resp.text, resp.url
    except requests.RequestException as e:
        print(f"  requests failed for {url}: {e}")
        return None, None


def _expand_accordions(page):
    """Click all accordion/dropdown/expandable elements to reveal hidden content."""
    try:
        page.evaluate("""() => {
            // Selectors for expandable elements
            const selectors = [
                '[class*="accordion"] [class*="title"]',
                '[class*="accordion"] [class*="header"]',
                '[class*="accordion"] [class*="trigger"]',
                '[class*="accordion"] h4',
                '[class*="accordion"] h3',
                'summary',
                '[aria-expanded="false"]',
                '[class*="collapse"]:not(.show) + [data-toggle]',
                '[data-toggle="collapse"]',
                '[class*="dropdown"] [class*="toggle"]',
                '[class*="expand"][role="button"]',
                '[class*="tab-item"]',
                'button[class*="accordion"]',
            ];
            const clicked = new Set();
            for (const sel of selectors) {
                for (const el of document.querySelectorAll(sel)) {
                    const key = el.textContent.trim().substring(0, 50);
                    if (clicked.has(key)) continue;
                    clicked.add(key);
                    try { el.click(); } catch(e) {}
                }
            }
        }""")
        import time
        time.sleep(1)

        # Scroll the full page to trigger lazy-loaded content
        page.evaluate("""async () => {
            const delay = ms => new Promise(r => setTimeout(r, ms));
            const height = document.body.scrollHeight;
            for (let y = 0; y < height; y += 500) {
                window.scrollTo(0, y);
                await delay(100);
            }
            window.scrollTo(0, 0);
        }""")
        import time
        time.sleep(1)
    except Exception:
        pass


def _fetch_page_playwright(url):
    """Fetch a page with Playwright (headless Chromium). Returns (html, final_url) or (None, None)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  Playwright not installed — skipping JS-rendered fallback.")
        return None, None

    print(f"  Falling back to Playwright for: {url}")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=USER_AGENT, ignore_https_errors=True,
            )
            page = context.new_page()
            page.goto(url, timeout=45000, wait_until="networkidle")

            # Expand all accordions/dropdowns and scroll for lazy content
            _expand_accordions(page)

            html = page.content()
            final_url = page.url
            browser.close()
            return html, final_url
    except Exception as e:
        print(f"  Playwright failed for {url}: {e}")
        return None, None


def _fetch_page(url, session):
    """Fetch a page, falling back to Playwright if content looks JS-rendered."""
    html, final_url = _fetch_page_requests(url, session)
    if html is None:
        # requests failed entirely — try Playwright
        html, final_url = _fetch_page_playwright(url)
    elif _looks_like_js_rendered(html):
        html, final_url = _fetch_page_playwright(url)
    return html, final_url


def _find_section_links(html, base_url):
    """Find links to IR sub-sections based on keyword matching."""
    soup = BeautifulSoup(html, "lxml")
    links = set()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        full_url = urljoin(base_url, href)

        # Only follow links on the same domain
        if urlparse(full_url).netloc != urlparse(base_url).netloc:
            continue

        # Check link text and surrounding context
        link_text = a_tag.get_text(separator=" ", strip=True).lower()
        title = (a_tag.get("title") or "").lower()
        aria_label = (a_tag.get("aria-label") or "").lower()
        searchable = f"{link_text} {title} {aria_label}"

        for keyword in ALL_SECTION_KEYWORDS:
            if keyword in searchable:
                links.add(full_url)
                break

    return links


def _find_downloadable_links(html, base_url, ir_url, since_year):
    """Find all links to downloadable files on a page.
    Only includes files on the same domain as ir_url and from since_year onward.
    Returns (same_domain_files, external_skipped)."""
    soup = BeautifulSoup(html, "lxml")
    files = set()
    external = set()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        full_url = urljoin(base_url, href)

        # Check by URL pattern
        is_dl = _is_downloadable(full_url)

        # Also check by CSS class (common on Asian corporate sites)
        if not is_dl:
            classes = " ".join(a_tag.get("class", []) or []).lower()
            if any(kw in classes for kw in ["download", "btn-download", "file-link"]):
                is_dl = True

        if not is_dl:
            continue
        if not _is_same_domain(full_url, ir_url):
            external.add(full_url)
            continue
        if not _is_since_year(full_url, since_year):
            continue
        files.add(full_url)

    return files, external


def _download_file_playwright(url, local_path):
    """Download a file using Playwright with a full browser context.
    Tries three strategies in order:
      1. Navigate and capture a download event (works for content-disposition).
      2. Navigate and read response body if the browser rendered/fetched the file.
      3. Use an API request from the browser context (carries cookies/headers).
    Returns True on success."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=USER_AGENT,
                accept_downloads=True,
                ignore_https_errors=True,
            )
            page = context.new_page()

            # Strategy 1: expect_download (content-disposition: attachment)
            try:
                with page.expect_download(timeout=30000) as download_info:
                    page.goto(url, timeout=45000)
                download = download_info.value
                download.save_as(str(local_path))
                browser.close()
                return True
            except Exception:
                pass

            # Strategy 2: page.goto returned content directly (e.g. PDF in browser)
            # Check the response by re-navigating and reading the body
            try:
                resp = page.goto(url, timeout=45000)
                if resp and resp.ok:
                    body = resp.body()
                    if len(body) > 0:
                        local_path.write_bytes(body)
                        browser.close()
                        return True
            except Exception:
                pass

            # Strategy 3: API request from the browser context (carries cookies)
            try:
                api_resp = context.request.get(url, timeout=45000)
                if api_resp.ok:
                    body = api_resp.body()
                    if len(body) > 0:
                        local_path.write_bytes(body)
                        browser.close()
                        return True
            except Exception:
                pass

            browser.close()
    except Exception:
        pass

    return False


def _download_file(url, dest_dir, session):
    """Download a single file. Returns (local_path, status)."""
    filename = _extract_filename(url)
    local_path = dest_dir / filename

    if local_path.exists():
        return local_path, "skipped"

    try:
        resp = session.get(url, timeout=60, stream=True)
        resp.raise_for_status()

        # Write in chunks to handle large files
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        return local_path, "downloaded"
    except requests.RequestException as e:
        # Retry with Playwright on 403/406 or SSL errors
        resp = getattr(e, "response", None)
        status_code = resp.status_code if resp is not None else None
        is_ssl = "SSL" in str(e) or "certificate" in str(e).lower()
        if status_code in (403, 406) or is_ssl:
            safe_name = filename.encode("ascii", errors="replace").decode()
            reason = f"SSL error" if is_ssl else f"got {status_code}"
            print(f"  {safe_name}: {reason}, retrying with Playwright...")
            if _download_file_playwright(url, local_path):
                return local_path, "downloaded (playwright)"
        return local_path, f"failed: {e}"


def scrape_ir(ticker, limit=DEFAULT_LIMIT, since_year=DEFAULT_SINCE_YEAR, deep=False):
    """Main entry point: crawl IR site for a ticker, download all documents.

    Args:
        ticker: Company ticker (must exist in companies.json).
        limit: Max number of files to download per run.
        since_year: Only download files whose URL/filename contains a year
                    >= since_year. Set to 0 to disable.
        deep: If True, follow links two levels deep instead of one.
    """
    companies = _load_companies()

    if ticker not in companies:
        print(f"Error: ticker '{ticker}' not found in companies.json.")
        print("  Run: python main.py add-company")
        return

    company = companies[ticker]
    ir_url = company.get("ir_url")
    if not ir_url:
        print(f"Error: no ir_url configured for '{ticker}'.")
        return

    print(f"=== IR Scraper: {ticker} — {company['name']} ===")
    print(f"IR URL: {ir_url}")
    print(f"Filters: limit={limit}, since={since_year}")

    # Prepare output directory
    dest_dir = DATA_DIR / ticker / "ir"
    dest_dir.mkdir(parents=True, exist_ok=True)

    session = _get_session()
    all_external_skipped = set()

    # --- Step 1: Fetch main IR page ---
    print(f"\nFetching main IR page...")
    main_html, main_final_url = _fetch_page(ir_url, session)
    if main_html is None:
        print(f"Error: could not reach IR page at {ir_url}")
        return

    base_url = main_final_url or ir_url

    # --- Step 2: Collect file links from main page ---
    all_file_urls, external = _find_downloadable_links(
        main_html, base_url, ir_url, since_year,
    )
    all_external_skipped |= external
    print(f"Found {len(all_file_urls)} downloadable file(s) on main page.")
    if external:
        print(f"  ({len(external)} external link(s) skipped)")

    visited_pages = [base_url]

    # --- Step 2b: Add any extra IR URLs from config ---
    extra_urls = company.get("ir_extra_urls", [])
    if extra_urls:
        print(f"\n{len(extra_urls)} extra IR page(s) configured.")
        for extra_url in extra_urls:
            print(f"\n  Fetching extra page: {extra_url}")
            extra_html, extra_final_url = _fetch_page(extra_url, session)
            if extra_html is None:
                print(f"  Could not fetch, skipping.")
                continue
            extra_base = extra_final_url or extra_url
            extra_files, extra_ext = _find_downloadable_links(
                extra_html, extra_base, ir_url, since_year,
            )
            new_files = extra_files - all_file_urls
            all_external_skipped |= extra_ext
            print(f"  Found {len(extra_files)} file(s) ({len(new_files)} new).")
            all_file_urls |= extra_files

            # Also follow sub-links from extra pages (one level deep)
            extra_section_links = _find_section_links(extra_html, extra_base)
            if extra_section_links:
                print(f"  Following {len(extra_section_links)} sub-link(s) from extra page...")
                for sub_url in sorted(extra_section_links):
                    if sub_url in visited_pages:
                        continue
                    visited_pages.append(sub_url)
                    sub_html, sub_final = _fetch_page(sub_url, session)
                    if sub_html is None:
                        continue
                    sub_base = sub_final or sub_url
                    sub_files, sub_ext = _find_downloadable_links(
                        sub_html, sub_base, ir_url, since_year,
                    )
                    sub_new = sub_files - all_file_urls
                    all_external_skipped |= sub_ext
                    if sub_new:
                        print(f"    {sub_url}: {len(sub_new)} new file(s)")
                    all_file_urls |= sub_files
                    time.sleep(DOWNLOAD_DELAY)

            time.sleep(DOWNLOAD_DELAY)

    # --- Step 3: Find and follow sub-section links ---
    section_links = _find_section_links(main_html, base_url)
    print(f"Found {len(section_links)} sub-section link(s) to follow.")

    for section_url in sorted(section_links):
        print(f"\n  Visiting sub-section: {section_url}")
        visited_pages.append(section_url)

        sub_html, sub_final_url = _fetch_page(section_url, session)
        if sub_html is None:
            print(f"  Could not fetch sub-section, skipping.")
            continue

        sub_base = sub_final_url or section_url
        sub_files, sub_external = _find_downloadable_links(
            sub_html, sub_base, ir_url, since_year,
        )
        new_files = sub_files - all_file_urls
        all_external_skipped |= sub_external
        print(f"  Found {len(sub_files)} file(s) ({len(new_files)} new).")
        if sub_external:
            print(f"  ({len(sub_external)} external link(s) skipped)")
        all_file_urls |= sub_files

        # Deep mode: follow links one more level from sub-sections
        if deep:
            level2_links = _find_section_links(sub_html, sub_base)
            for l2_url in sorted(level2_links):
                if l2_url in visited_pages:
                    continue
                visited_pages.append(l2_url)
                l2_html, l2_final = _fetch_page(l2_url, session)
                if l2_html is None:
                    continue
                l2_base = l2_final or l2_url
                l2_files, l2_ext = _find_downloadable_links(
                    l2_html, l2_base, ir_url, since_year,
                )
                l2_new = l2_files - all_file_urls
                all_external_skipped |= l2_ext
                if l2_new:
                    print(f"    [deep] {l2_url}: {len(l2_new)} new file(s)")
                all_file_urls |= l2_files
                time.sleep(DOWNLOAD_DELAY)

        time.sleep(DOWNLOAD_DELAY)

    # --- Step 4: Download all files (respecting limit) ---
    sorted_urls = sorted(all_file_urls)
    print(f"\n--- Downloading up to {limit} of {len(sorted_urls)} file(s) to {dest_dir} ---")

    downloaded = 0
    skipped = 0
    failed = 0
    hit_limit = False
    log_entries = []

    for file_url in sorted_urls:
        # Check download limit (count new downloads + failures against the cap)
        if downloaded + failed >= limit:
            hit_limit = True
            break

        filename = _extract_filename(file_url)
        local_path, status = _download_file(file_url, dest_dir, session)

        safe_name = filename.encode("ascii", errors="replace").decode()
        safe_url = file_url.encode("ascii", errors="replace").decode()
        if status.startswith("downloaded"):
            via = " (via Playwright)" if "playwright" in status else ""
            print(f"  Downloading: {safe_name}{via} from {safe_url}")
            downloaded += 1
            time.sleep(DOWNLOAD_DELAY)
        elif status == "skipped":
            skipped += 1
        else:
            print(f"  FAILED: {safe_name} — {status}")
            failed += 1

        log_entries.append({
            "url": file_url,
            "local_path": str(local_path),
            "filename": filename,
            "status": status,
        })

    # Log external links that were skipped
    for ext_url in sorted(all_external_skipped):
        log_entries.append({
            "url": ext_url,
            "local_path": "",
            "filename": _extract_filename(ext_url),
            "status": "external link skipped",
        })

    # --- Step 5: Summary ---
    print(f"\n=== Summary ===")
    print(f"Downloaded {downloaded} new file(s).")
    print(f"{skipped} file(s) already existed (skipped).")
    if failed:
        print(f"{failed} file(s) failed.")
    if all_external_skipped:
        print(f"{len(all_external_skipped)} external link(s) skipped (off-domain).")
    if hit_limit:
        remaining = len(sorted_urls) - downloaded - skipped
        print(f"\nHit download limit of {limit} files. "
              f"Run with --limit N to adjust. ({remaining} file(s) remaining)")

    # --- Step 6: Write download log ---
    log_path = dest_dir / "download_log.json"
    log_data = {
        "ticker": ticker,
        "company": company["name"],
        "ir_url": ir_url,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "filters": {"limit": limit, "since_year": since_year},
        "pages_visited": visited_pages,
        "files": log_entries,
        "summary": {
            "downloaded": downloaded,
            "skipped": skipped,
            "failed": failed,
            "external_skipped": len(all_external_skipped),
            "total_candidates": len(all_file_urls),
            "hit_limit": hit_limit,
        },
    }
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log_data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Download log saved to: {log_path}")
