#!/usr/bin/env python3
"""Capture N live captcha images to /tmp/capview for view-accuracy scoring.

The captcha only needs the page to have rendered -- no form filling, no
selection. This captures the rendered <img> as a screenshot of the element,
which measures exactly what a human solving the captcha sees.

Run:
    python scripts/grab_captchas.py [N]
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

load_dotenv(dotenv_path=str(ROOT / ".env"))
BASE_URL = "https://voters.eci.gov.in/download-eroll"
OUT = Path("/tmp/capview")


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    OUT.mkdir(parents=True, exist_ok=True)
    got = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()
        print("[*] loading page")
        # Do NOT hand-roll the goto/wait. The Angular app boots only after the
        # 1.8MB main bundle lands, which measured anywhere from 0.6s to 80s and
        # sometimes never lands at all. A bare page.goto + one 120s wait timed
        # out on #stateCode here while test_full_flow.py -- which retries up to
        # PAGE_LOAD_ATTEMPTS -- succeeds on the first try. Reuse that.
        from src.eci_downloader import ECIDownloader
        d = ECIDownloader()
        if not d._open_form(page):
            print("[!] form never loaded after all retries")
            browser.close()
            return 1
        print("[+] page rendered")

        # The captcha is NOT in the initial DOM -- it only renders once the
        # dropdown chain has been filled, because the form enables download only
        # after a constituency and language are chosen. Waited 120s for #captcha
        # on a bare page load and it never came. So fill the chain first, using
        # the same sequence test_full_flow.py proves works.
        def sel(selector, label, value=None):
            if value is None:
                value = d._find_option_value(page, selector, label)
            if value is None:
                print(f"  [!] no option for {selector} {label!r}")
                return False
            page.select_option(selector, value=value)
            page.wait_for_load_state("networkidle")
            time.sleep(2)
            print(f"  [+] {selector} = {value}")
            return True

        sel("#stateCode", "", "S01")
        sel("#revyear", "", "2026")
        sel("#roleType", "SIR DraftRoll - 2026")
        sel("#district", "Srikakulam")
        sel("#constituency", "1 - Ichchapuram")
        sel("#langCd", "ENGLISH")

        page.wait_for_selector("#captcha", timeout=120000)
        print("[+] captcha rendered")
        img = page.locator("#captcha").first
        for i in range(1, n + 1):
            try:
                dest = OUT / f"cap{i}.jpg"
                img.screenshot(path=str(dest))
                got += 1
                print(f"  [+] {dest} ({dest.stat().st_size:,} bytes)")
            except Exception as exc:
                print(f"  [!] {i}: {type(exc).__name__}: {exc}")
            # refresh for the next one
            try:
                page.click("text=/refr?sh\\s+captcha/i", timeout=5000)
            except Exception:
                try:
                    page.click("img[alt*='aptcha']", timeout=5000)
                except Exception as exc2:
                    print(f"      [!] refresh failed: {type(exc2).__name__}")
                    break
            time.sleep(2)
        browser.close()
    print(f"[*] captured {got}/{n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())