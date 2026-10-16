"""One real download attempt, end to end, through the corrected code path.

This is the demo entry point. It drives the real site, solves the real captcha,
builds the request body the SPA actually builds, follows the returned path, and
saves whatever comes back. It never simulates.

Run:  .venv-test/bin/python scripts/live_attempt.py
"""
import base64
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from playwright.sync_api import sync_playwright  # noqa: E402

from eci_downloader import (  # noqa: E402
    ECIDownloader, BASE_URL, HEADLESS, MIS_KEY, EROLL_MEDIA_BASE,
)

# The combination used throughout the investigation: Karol Bagh, Delhi. Chosen
# because every dropdown value for it was verified live.
PARAMS = {
    "state": "U05",
    "year": "2025",
    "rollType": "Final Roll - 2025",
    "district": "Central",
    "constituency": "23 - KAROL BAGH",
    "language": "ENGLISH",
}
LABELS = ("NCT OF Delhi (Delhi)", "2025", "Final Roll - 2025",
          "Central", "23 - KAROL BAGH", "ENGLISH")


def pick(page, selector, label):
    page.wait_for_selector(f"{selector} option[value]", state="attached",
                           timeout=90000)
    for opt in page.locator(f"{selector} option").all():
        if opt.inner_text().strip() == label:
            return opt.get_attribute("value")
    return None


def main():
    d = ECIDownloader()
    out = ROOT / "downloaded_pdfs"
    out.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        d.attach_status_probe(page)

        print("=" * 70)
        print("STEP 1  open the form")
        if not d._open_form(page):
            print("FAILED: form never became available")
            return 1
        print("   ok")

        print("\nSTEP 2  fill the form and submit (real network, real captcha)")
        ok, result = d.download_all_parts_for_combination(
            page, PARAMS, *LABELS)
        print(f"\nSTEP 3  result: {ok} {result}")

        if ok and Path(result).exists():
            data = Path(result).read_bytes()
            print(f"   {len(data):,} bytes")
            print(f"   first 4 bytes: {data[:4]!r} "
                  f"({'PDF' if data[:4] == b'%PDF' else 'ZIP' if data[:4] == b'PK' else 'unknown'})")
            return 0
        return 1


if __name__ == "__main__":
    sys.exit(main())
